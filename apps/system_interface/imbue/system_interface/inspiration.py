"""In-mind inspiration publishing: the request/confirm handshake between the
`/publish-inspiration` skill and the frontend publish modal.

The publishing flow spans three processes and is coordinated entirely through
local state plus a response file on disk:

1. The `/publish-inspiration` skill assembles a clean commit, then POSTs an
   `InspirationPublishRequest` to `/api/inspiration/publish-request`. That
   handler calls `record_request`, which stores the pending slug and
   broadcasts an `inspiration_publish_requested` event over the WebSocket
   broadcaster so the frontend opens the publish modal pre-filled with the
   proposed title / description / repo name / visibility / thumbnail.

2. The user edits and confirms (or aborts) in the modal. Confirm POSTs an
   `InspirationPublishConfirm` to `/api/inspiration/publish-confirm`, which
   calls `confirm`; abort (or closing the modal) calls `abort`. Either way,
   an `InspirationPublishResponse` is written to a fixed absolute response
   file (`/code/runtime/inspiration/publish-response.json`).

3. The skill, having POSTed the request, polls that same response file. Once
   it appears it reads the `InspirationPublishResponse`: `status="confirmed"`
   means proceed with `gh repo create` + push using the user-edited fields;
   `status="aborted"` means stop and leave the assembled commit intact.

The response file is the only cross-process channel back to the skill, so its
path must be absolute and agreed by both sides -- it is deliberately NOT
`Path.cwd()`-relative (the skill and the server may run from different cwds).

The SVG thumbnail supplied in the request is untrusted markup. The frontend
sanitizes it with dompurify for the live preview; the backend additionally
strips dangerous constructs server-side in `confirm` before the value is
written to the response file, so the value the skill ultimately commits is
already cleaned regardless of what the frontend did.

The broadcaster is injected at construction (as a plain callable) so tests can
record broadcasts without a live WebSocket, matching the dependency-injection
style used by `ClaudeAuthService`.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger as _loguru_logger
from pydantic import PrivateAttr

from imbue.imbue_common.mutable_model import MutableModel
from imbue.system_interface.models import InspirationPublishConfirm
from imbue.system_interface.models import InspirationPublishRequest
from imbue.system_interface.models import InspirationPublishResponse
from imbue.system_interface.models import InspirationStatusResponse

logger = _loguru_logger

# The container repo root is `/code` (see CLAUDE.md: cwd = repo root), and
# per-feature runtime state lives under `runtime/<feature>/`. This absolute
# path is the cross-process handshake location the polling skill agrees on; it
# is intentionally not derived from the current working directory.
_RESPONSE_DIR: Final = Path("/code/runtime/inspiration")
_RESPONSE_FILENAME: Final = "publish-response.json"

# The response file carries no secret, but chmod 600 matches the credential-file
# hygiene convention used elsewhere in this app and costs nothing.
_RESPONSE_FILE_MODE: Final = 0o600

# Untrusted-SVG stripping. dompurify handles the live preview in the frontend;
# these deterministic strips are the backend's defense-in-depth on `confirm`
# before the (possibly user-edited) markup is written to the response file and
# ultimately committed. We remove three classes of active content:
#   - <script> ... </script> elements (any casing / attributes)
#   - on* event-handler attributes (onload=, onclick=, ...)
#   - <foreignObject> ... </foreignObject> (an HTML-injection vector inside SVG)
_SCRIPT_ELEMENT_REGEX: Final = re.compile(r"<script\b[^>]*>.*?</script\s*>", re.IGNORECASE | re.DOTALL)
_SELF_CLOSING_SCRIPT_REGEX: Final = re.compile(r"<script\b[^>]*/>", re.IGNORECASE)
_FOREIGN_OBJECT_REGEX: Final = re.compile(r"<foreignObject\b[^>]*>.*?</foreignObject\s*>", re.IGNORECASE | re.DOTALL)
_SELF_CLOSING_FOREIGN_OBJECT_REGEX: Final = re.compile(r"<foreignObject\b[^>]*/>", re.IGNORECASE)
_EVENT_HANDLER_ATTR_REGEX: Final = re.compile(
    r"""\son[a-zA-Z]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""",
    re.IGNORECASE,
)


class InspirationError(RuntimeError):
    """Raised when an inspiration publish flow operation cannot complete."""


def sanitize_svg(svg: str) -> str:
    """Strip active content from untrusted SVG markup before it is persisted.

    Deterministically removes `<script>` elements, `on*` event-handler
    attributes, and `<foreignObject>` elements. This is the authoritative
    server-side strip applied on `confirm`; the frontend uses dompurify for the
    live preview but the backend does not rely on that having happened.
    """
    cleaned = _SCRIPT_ELEMENT_REGEX.sub("", svg)
    cleaned = _SELF_CLOSING_SCRIPT_REGEX.sub("", cleaned)
    cleaned = _FOREIGN_OBJECT_REGEX.sub("", cleaned)
    cleaned = _SELF_CLOSING_FOREIGN_OBJECT_REGEX.sub("", cleaned)
    cleaned = _EVENT_HANDLER_ATTR_REGEX.sub("", cleaned)
    return cleaned


def _publish_requested_event(req: InspirationPublishRequest) -> dict[str, Any]:
    """Build the `inspiration_publish_requested` broadcast payload.

    `thumbnail_svg` is untrusted; the frontend sanitizes it with dompurify
    before rendering the live preview.
    """
    return {
        "type": "inspiration_publish_requested",
        "slug": req.slug,
        "title": req.title,
        "description": req.description,
        "repo_name": req.repo_name,
        "visibility": req.visibility,
        "thumbnail_svg": req.thumbnail_svg,
    }


def _publish_aborted_event(slug: str | None) -> dict[str, Any]:
    """Build the `inspiration_publish_aborted` broadcast payload.

    The frontend guards on `slug`: it only closes the modal if the aborted
    slug matches the proposal currently shown.
    """
    return {"type": "inspiration_publish_aborted", "slug": slug}


class InspirationService(MutableModel):
    """Coordinates the inspiration publish request/confirm handshake.

    Holds the single pending proposal slug (only one publish proposal can be
    open at a time, matching the single-mind / single-user deployment model)
    and writes the confirm/abort outcome to the fixed response file the polling
    skill reads. One instance is created per application and stored on
    `app.state`; `broadcast` is injected (in production, `state.broadcaster.broadcast`)
    so this class does no networking itself.
    """

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    response_dir: Path = _RESPONSE_DIR
    broadcast: Callable[[dict[str, Any]], None]

    # The pending slug and the lock guarding it are private runtime state, not
    # configuration data. A single lock serializes record/confirm/abort so the
    # response file and the pending slug never disagree.
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _pending_slug: str | None = PrivateAttr(default=None)

    def _response_path(self) -> Path:
        return self.response_dir / _RESPONSE_FILENAME

    def _write_response_file(self, response: InspirationPublishResponse) -> None:
        """Serialize `response` to the fixed response file with mode 0o600.

        The skill polls for this file's existence, so any stale copy is removed
        by `record_request` before a new request goes out; this method only
        ever writes the terminal (confirmed / aborted) outcome.
        """
        self.response_dir.mkdir(parents=True, exist_ok=True)
        path = self._response_path()
        path.write_text(json.dumps(response.model_dump()))
        path.chmod(_RESPONSE_FILE_MODE)

    def record_request(self, req: InspirationPublishRequest) -> None:
        """Record a new pending proposal and broadcast it to the frontend.

        Deletes any stale response file from a prior proposal (so the skill's
        poll for *this* proposal doesn't immediately read an old outcome),
        stores the pending slug, then broadcasts `inspiration_publish_requested`
        so the modal opens pre-filled. A new request supersedes any prior one.
        """
        with self._lock:
            self._pending_slug = req.slug
            stale = self._response_path()
            if stale.exists():
                stale.unlink()
        self.broadcast(_publish_requested_event(req))

    def confirm(self, confirm: InspirationPublishConfirm) -> InspirationPublishResponse:
        """Persist the user-confirmed (sanitized) values for the skill to read.

        The confirm's slug must match the pending proposal, otherwise the
        confirm is for a superseded / nonexistent proposal and is rejected. The
        SVG is stripped server-side here so the value the skill commits is
        already clean. Clears the pending slug on success.
        """
        with self._lock:
            if self._pending_slug is None or confirm.slug != self._pending_slug:
                raise InspirationError(
                    f"No pending inspiration proposal matches slug {confirm.slug!r}"
                    f" (pending: {self._pending_slug!r})"
                )
            response = InspirationPublishResponse(
                status="confirmed",
                slug=confirm.slug,
                title=confirm.title,
                description=confirm.description,
                repo_name=confirm.repo_name,
                visibility=confirm.visibility,
                thumbnail_svg=sanitize_svg(confirm.thumbnail_svg),
            )
            self._write_response_file(response)
            self._pending_slug = None
        return response

    def abort(self, slug: str | None = None) -> None:
        """Record an aborted outcome so the skill's poll unblocks, then broadcast.

        Writes an `aborted` response for the given slug (defaulting to the
        pending slug) so the polling skill stops instead of hanging, broadcasts
        `inspiration_publish_aborted` (slug-guarded on the frontend), and clears
        the pending slug.
        """
        with self._lock:
            aborted_slug = slug if slug is not None else self._pending_slug
            self._write_response_file(InspirationPublishResponse(status="aborted", slug=aborted_slug or ""))
            self._pending_slug = None
        self.broadcast(_publish_aborted_event(aborted_slug))

    def status(self) -> InspirationStatusResponse:
        """Report whether a publish proposal is currently awaiting user action."""
        with self._lock:
            pending = self._pending_slug
        return InspirationStatusResponse(pending_slug=pending, has_pending=pending is not None)
