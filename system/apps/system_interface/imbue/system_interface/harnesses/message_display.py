"""Classify a ``user_message`` into its render decision (the ``display`` wire fields).

The ONE detector table that turns a raw user message into a :class:`DisplayKind` (+ chip
label + display body). Every harness's parser calls :func:`classify_user_message` when it
emits a ``user_message``; the frontend renders the returned decision and never re-sniffs
the text. This replaces the frontend's detector table in ``message-classification.ts`` AND
the hand-maintained copy the activity path kept in ``activity_state.py`` -- one
implementation, both consumers.

NOT per-harness, deliberately: every harness's sentinels live in this same list, because
they are distinctive enough (a ``/welcome``, a ``Stop hook feedback:`` header, a ``/model``
command, a browser-fleet tag) never to collide across harnesses. Adding a harness = append
ITS detectors here; a detector only some harnesses emit simply never fires for the rest.

Order of decision (:func:`classify_user_message`):

1. An explicit detector matches (stop hook, fleet, task-notification, skill, /welcome,
   model-bar traffic, a latchkey resolution) -> that decision. Explicit detectors WIN over
   ``is_meta`` -- Stop-hook feedback is ``is_meta`` yet deliberately surfaces as a chip.
2. else ``is_compact_summary`` (claude's flag on the record injected after
   auto-compaction) -> a labelled chip; keyed off the structural flag, not the summary
   text, so wording changes never break it.
3. else ``is_meta`` (claude's flag for a framework-injected, model-only message) ->
   hidden. One rule hides the whole family, present and future.
4. else -> no decision (a genuine human turn; the parser emits no ``display`` field).
"""

import re
from typing import Any

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.pure import pure
from imbue.system_interface.harnesses.events import DisplayKind

# Cross-layer contract: the sentinel the agentic browser fleet wraps its agent-facing
# nudges in before sending them via ``mngr message``. The wrapping side is
# ``system/apps/browser/src/browser/session.py`` (``_SYSTEM_MESSAGE_TAG``); keep in sync.
BROWSER_FLEET_TAG = "agentic-browser-fleet"

_SKILL_EXPANSION_PREFIX = "Base directory for this skill:"
_SKILL_NAME_RE = re.compile(r"skills/([^\n/]+)")
_STOP_HOOK_PREFIX = "Stop hook feedback:\n"
_TASK_NOTIFICATION_OPEN = "<task-notification>"
_TASK_NOTIFICATION_PREAMBLE = "[SYSTEM NOTIFICATION"
# Anchored, DOTALL match of the fleet sentinel wrapping the whole message. We control the
# format, so an exact match is safe.
_BROWSER_FLEET_RE = re.compile(rf"^\s*<{BROWSER_FLEET_TAG}>([\s\S]*)</{BROWSER_FLEET_TAG}>\s*$")
# The composer's model bar drives its harness with /model, /effort, and /fast slash
# commands; the harness records the command plus a <local-command-stdout> confirmation,
# and never a model reply -- neither is a conversational turn.
_COMPOSER_COMMAND_RE = re.compile(r"^/(model|fast|effort)\b")
_LOCAL_COMMAND_STDOUT_OPEN = "<local-command-stdout>"
_COMPOSER_STDOUT_RE = re.compile(r"Set model to|Set effort level to|Fast mode")

# Chip label for the post-auto-compaction summary.
_COMPACTION_SUMMARY_LABEL = "Summary of earlier conversation"

# The composer appends a "See attachment(s) here:" block to a message it sends with
# uploads (see frontend/src/models/attachments.ts -- keep the two in step). Detectors run
# on the text BEFORE the block, so an appended attachment never changes a message's kind
# (the whole-string-anchored detectors -- /welcome, the fleet sentinel -- would otherwise
# miss), and a chip's body shows the message text, not the raw attachment markdown.
_ATTACHMENT_BLOCK_RE = re.compile(r"(?:^|\n\n)(See attachments? here: ([\s\S]+?))\s*$")
_UPLOADS_PATH_MARKER = "/uploads/"


def _visible_text(content: str) -> str:
    """The message text before any trailing attachment block (the whole content when none)."""
    match = _ATTACHMENT_BLOCK_RE.search(content)
    if match is None:
        return content
    items = [item.strip() for item in match.group(2).split("\n")]
    if not all(_UPLOADS_PATH_MARKER in item for item in items):
        return content
    return content[: match.start()].rstrip()


# When a latchkey permission request is resolved, the app injects a plain user message
# announcing the outcome. The phrasing is authored by the latchkey handlers in the mngr
# repo (apps/minds/imbue/minds/desktop_client/latchkey/handlers/) -- a copy edit there
# strands cards here, so keep the two in step. The patterns require only
# "Your ... request ... was granted/denied" (anchored to the start), because the exact
# phrasing differs per request type; the frontend only consults the decision while a
# request is actually awaiting one, so a prose look-alike stays unlikely.
_RESOLUTION_GRANTED_RE = re.compile(r"^Your\b.*\brequest\b.*\bwas granted\b")
_RESOLUTION_DENIED_RE = re.compile(r"^Your\b.*\brequest\b.*\bwas denied\b")
_RESOLUTION_ERROR_RE = re.compile(r"^Your\b.*\brequest\b.*\bcould not be completed\b")


class MessageDisplay(FrozenModel):
    """One user message's render decision, as it goes on the wire."""

    display: DisplayKind
    # Chip title (CHIP) or skill name (SKILL_EXPANSION); omitted otherwise.
    display_label: str | None = None
    # The body to display when a wrapper sentinel was stripped (a fleet nudge); omitted
    # when the raw content is already the display body.
    display_body: str | None = None
    # PERMISSION_RESOLUTION only: granted / denied / error.
    resolution: str | None = Field(default=None, pattern="^(granted|denied|error)$")

    def apply_to(self, event: dict[str, Any]) -> None:
        """Stamp the decision's present fields onto ``event`` (absent fields stay absent)."""
        event.update(self.model_dump(mode="json", exclude_none=True))


def _match_welcome(content: str) -> MessageDisplay | None:
    """The seeded ``/welcome`` invocation the desktop client sends every new agent."""
    if content.strip() != "/welcome":
        return None
    return MessageDisplay(display=DisplayKind.HIDDEN)


def _match_skill_expansion(content: str) -> MessageDisplay | None:
    """A skill expansion; its body folds into the preceding Skill tool-call block."""
    if not content.startswith(_SKILL_EXPANSION_PREFIX):
        return None
    match = _SKILL_NAME_RE.search(content)
    return MessageDisplay(
        display=DisplayKind.SKILL_EXPANSION,
        display_label=match.group(1) if match is not None else None,
    )


def _match_stop_hook(content: str) -> MessageDisplay | None:
    """Stop-hook feedback the harness injects when a Stop hook fires."""
    if not content.startswith(_STOP_HOOK_PREFIX):
        return None
    return MessageDisplay(display=DisplayKind.CHIP, display_label="Stop hook feedback")


def _match_task_notification(content: str) -> MessageDisplay | None:
    """A background-task completion notice, bare or behind a [SYSTEM NOTIFICATION] preamble."""
    trimmed = content.lstrip()
    is_notice = trimmed.startswith(_TASK_NOTIFICATION_OPEN) or (
        trimmed.startswith(_TASK_NOTIFICATION_PREAMBLE) and _TASK_NOTIFICATION_OPEN in content
    )
    if not is_notice:
        return None
    return MessageDisplay(display=DisplayKind.CHIP, display_label="Background task")


def _match_browser_fleet(content: str) -> MessageDisplay | None:
    """A browser-fleet nudge; the sentinel is stripped so the chip shows the inner text."""
    match = _BROWSER_FLEET_RE.match(content)
    if match is None:
        return None
    return MessageDisplay(display=DisplayKind.CHIP, display_label="Browser fleet", display_body=match.group(1).strip())


def _match_composer_command(content: str) -> MessageDisplay | None:
    """A /model, /effort, or /fast slash command the model bar (or the user) sent."""
    if _COMPOSER_COMMAND_RE.match(content.strip()) is None:
        return None
    return MessageDisplay(display=DisplayKind.HIDDEN)


def _match_composer_command_output(content: str) -> MessageDisplay | None:
    """The ``<local-command-stdout>`` confirmation for a model-bar command."""
    trimmed = content.lstrip()
    if not trimmed.startswith(_LOCAL_COMMAND_STDOUT_OPEN):
        return None
    if _COMPOSER_STDOUT_RE.search(content) is None:
        return None
    return MessageDisplay(display=DisplayKind.HIDDEN)


def _match_permission_resolution(content: str) -> MessageDisplay | None:
    """A latchkey permission-request verdict, injected as a plain user message."""
    if _RESOLUTION_GRANTED_RE.search(content) is not None:
        resolution = "granted"
    elif _RESOLUTION_DENIED_RE.search(content) is not None:
        resolution = "denied"
    elif _RESOLUTION_ERROR_RE.search(content) is not None:
        resolution = "error"
    else:
        return None
    return MessageDisplay(display=DisplayKind.PERMISSION_RESOLUTION, resolution=resolution)


# Most-specific first; classify_user_message takes the first match.
_DETECTORS = (
    _match_welcome,
    _match_skill_expansion,
    _match_stop_hook,
    _match_task_notification,
    _match_browser_fleet,
    _match_composer_command,
    _match_composer_command_output,
    _match_permission_resolution,
)


@pure
def classify_user_message(
    content: str, *, is_meta: bool = False, is_compact_summary: bool = False
) -> MessageDisplay | None:
    """The render decision for one user message, or ``None`` for a genuine human turn.

    ``None`` means the parser emits no ``display`` field and the frontend renders the
    baseline user bubble. Detectors run on the attachment-stripped text (see
    ``_visible_text``); see the module docstring for the precedence rules.
    """
    visible = _visible_text(content)
    decision: MessageDisplay | None = None
    for detect in _DETECTORS:
        decision = detect(visible)
        if decision is not None:
            break
    if decision is None and is_compact_summary:
        decision = MessageDisplay(display=DisplayKind.CHIP, display_label=_COMPACTION_SUMMARY_LABEL)
    if decision is None and is_meta:
        decision = MessageDisplay(display=DisplayKind.HIDDEN)
    if decision is None:
        return None
    # A chip renders its body verbatim; when an attachment block was stripped for
    # classification, show the message text rather than the raw attachment markdown.
    if decision.display is DisplayKind.CHIP and decision.display_body is None and visible != content:
        decision = decision.model_copy_update(to_update(decision.field_ref().display_body, visible))
    return decision


def stamp_user_message_display(
    event: dict[str, Any], content: str, *, is_meta: bool = False, is_compact_summary: bool = False
) -> None:
    """Stamp the wire's render-decision fields onto one ``user_message`` event.

    The ONE call every user-message emit site makes (each harness's normal path AND
    claude's queued-command attachment path), so a new wire field or a precedence change
    lands everywhere at once instead of in per-parser copies.
    """
    decision = classify_user_message(content, is_meta=is_meta, is_compact_summary=is_compact_summary)
    if decision is not None:
        decision.apply_to(event)
    if is_non_turn_tail(content, is_meta=is_meta):
        event["non_turn_tail"] = True


@pure
def is_non_turn_tail(content: str, *, is_meta: bool = False) -> bool:
    """True for a user message that is NOT a genuine turn awaiting a reply.

    The activity path's signal: a transcript ending on one of these must not pin the
    indicator on "Thinking...", since no model reply is coming for it. Two signals, exactly
    the pre-existing set: the harness's own framework flag (``is_meta``), and model-bar
    traffic (a /model, /effort, or /fast command or its confirmation -- the harness handles
    those locally and never replies). Deliberately NOT derived from :class:`DisplayKind`:
    display is how a message renders, this is whether a reply follows, and the two disagree
    (a hidden ``/welcome`` gets a reply; an is_meta stop-hook chip does not).
    """
    if is_meta:
        return True
    # The two model-bar detectors themselves, so this can never drift from rendering.
    return _match_composer_command(content) is not None or _match_composer_command_output(content) is not None
