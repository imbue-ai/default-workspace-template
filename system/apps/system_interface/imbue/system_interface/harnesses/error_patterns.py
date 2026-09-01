"""Classify a model API error out of an agent's failure text.

When a request to the model API fails, each harness writes the failure somewhere in its
transcript, in its own surface form:

* claude: a synthetic assistant message, ``API Error: 529 Overloaded``, sometimes carrying
  the raw error JSON.
* pi: an assistant message with empty ``content`` and the whole failure in a sibling
  ``errorMessage`` -- a bare ``"<status> <json>"``, e.g.
  ``400 {"type":"error","error":{"type":"invalid_request_error",...}}``.
* codex: a ``task_complete`` event carrying ``error.message``, usually alongside a structured
  ``codex_error_info`` its parser prefers over this.

The kind set and the provider-fault split are ordinary HTTP semantics, so they are
harness-agnostic; only the surface-form regexes differ, and both live below.

Auth-family failures are handled by :mod:`auth_errors`, which has its own recovery surface --
so they are deliberately NOT classified here. That is enforced rather than left to the tables:
:func:`classify_api_error` returns ``None`` for anything the auth vocabulary claims, so a
message can never carry both subtexts. Without it the two families overlap by construction --
Anthropic reports exhausted third-party usage as a 400 ``invalid_request_error``, which is in
BOTH this module's type table and the auth one.

The kind set and the provider-fault split are ordinary HTTP semantics, so they
are harness-agnostic; only the two surface-form regexes are Claude-shaped. A
second harness that surfaces provider errors differently classifies them in its
own parser and stamps the same event fields (``is_api_error`` /
``api_error_kind`` / ``is_provider_fault``), which is the shared contract the
frontend reads.
"""

from __future__ import annotations

import re

from imbue.system_interface.harnesses.auth_errors import is_auth_error_text

# HTTP status -> normalized kind. Sourced from the Anthropic API errors reference
# (platform.claude.com/docs/en/api/errors). 401 is intentionally omitted -- auth is
# handled by auth_errors.py.
_STATUS_KINDS: dict[str, str] = {
    "400": "invalid_request",
    "403": "permission",
    "404": "not_found",
    "413": "request_too_large",
    "429": "rate_limit",
    "500": "api_error",
    "503": "overloaded",
    "529": "overloaded",
}

# The ``"type": "<x>_error"`` form Claude sometimes embeds in the failure text.
# ``authentication_error`` is intentionally absent (handled by auth_errors.py).
_TYPE_KINDS: dict[str, str] = {
    "invalid_request_error": "invalid_request",
    "permission_error": "permission",
    "not_found_error": "not_found",
    "request_too_large": "request_too_large",
    "rate_limit_error": "rate_limit",
    "api_error": "api_error",
    "overloaded_error": "overloaded",
}

# Kinds that mean the model provider's servers failed, not our request -- these
# earn the "not Minds' fault" note on the frontend.
_PROVIDER_FAULT_KINDS: frozenset[str] = frozenset({"api_error", "overloaded"})

# ``API Error: <code> ...`` -- the surface form Claude Code writes for a failed request.
_API_ERROR_STATUS_RE = re.compile(r"API Error:\s*(\d{3})\b", re.IGNORECASE)
# pi's bare ``<status> {json}``, with no prefix at all. ANCHORED to the start of the string:
# unanchored, any message that happens to quote a status code mid-sentence reads as a failure.
_BARE_STATUS_RE = re.compile(r"^\s*(\d{3})\s*[{\s]", re.IGNORECASE)
# ``"type": "<x>_error"`` -- the embedded raw-error form.
_API_ERROR_TYPE_RE = re.compile(r'"type"\s*:\s*"([a-z_]+error)"', re.IGNORECASE)


def classify_api_error(text: str) -> str | None:
    """Return a normalized API-error kind for ``text``, or ``None`` when it is not
    a recognized model API error.

    Auth errors return ``None`` here on purpose (see the module docstring): the two families
    have different recovery surfaces, and a message carrying both subtexts would offer the
    user two contradictory next steps.
    """
    if not text or is_auth_error_text(text):
        return None
    status_match = _API_ERROR_STATUS_RE.search(text) or _BARE_STATUS_RE.match(text)
    if status_match is not None:
        status_kind = _STATUS_KINDS.get(status_match.group(1))
        if status_kind is not None:
            return status_kind
    type_match = _API_ERROR_TYPE_RE.search(text)
    if type_match is not None:
        type_kind = _TYPE_KINDS.get(type_match.group(1).lower())
        if type_kind is not None:
            return type_kind
    return None


def is_provider_fault(kind: str | None) -> bool:
    """True when ``kind`` is a model-provider-side failure (a 5xx / overloaded)
    rather than a client-side one -- the ones that earn the "not Minds' fault"
    note."""
    return kind in _PROVIDER_FAULT_KINDS
