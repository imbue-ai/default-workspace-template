"""The structured facts stamped out of a tool result at parse time.

Tool outputs are not resident and never cross the wire (the payload-free event contract in
:mod:`harnesses.events`); whatever the default chat render needs from an output is lifted
out here, whole, while the parser still holds the raw text. Harness-neutral, shared by
every parser:

- **Latchkey permission requests.** An agent asks the user for permission by POSTing to
  the reserved ``latchkey-self.invalid/permission-requests`` host (see the latchkey
  skill); the gateway echoes the created request back on stdout as a JSON object. The
  mechanism is harness-agnostic -- the agent just runs curl -- so every parser detects the
  call from its input (:func:`is_permission_request_call` ->
  ``tool_call.display = "permission_request"``) and lifts the echoed object out of the
  result (:func:`find_permission_request` -> the event's ``permission_request`` field), so
  the card renders from structured data without fetching the output.

  Both readers assume one filing per tool call, with the echo in that call's own
  result. ``system/scripts/agent_latchkey_request_standalone.sh`` and its checker
  ``agent_latchkey_request_check.py`` are what hold the agent to it, on every harness;
  they exist for these two functions and copy ``PERMISSION_REQUEST_HOST`` from here, so
  changing what counts as a request call -- or how many a result can carry -- means
  revisiting that gate too.

- **tk step decoration.** tk lifecycle commands print machine-readable decoration on
  stdout (``Created <id>: <title>``, ``Updated <id> -> <status>``,
  ``tk-step <id> title|summary: ...``) that the chat progress view reads back from the
  transcript. :func:`tk_stamp` lifts exactly those lines (plus any line carrying a step-id
  token, which the historical input fallback's positional id-title zipping needs) into the
  resident ``tk_stamp`` field. The format is defined in ``system/vendor/tk/ticket``; keep
  the two in sync.

- **Error snippets.** A failed call must stay glanceable without a fetch, so
  :func:`error_snippet` keeps its first line resident.
"""

import json
import re
from typing import Any
from typing import Final

from loguru import logger
from pydantic import Field
from tk_command_parsing.parser import parse_command

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.system_interface.harnesses.events import DisplayKind
from imbue.system_interface.harnesses.events import MAX_ERROR_SNIPPET_LENGTH
from imbue.system_interface.harnesses.events import MAX_TK_STAMP_LENGTH

# The reserved latchkey host an agent POSTs to when asking the user to approve an action.
# Deliberately short enough to survive even a truncated input preview.
PERMISSION_REQUEST_HOST = "latchkey-self.invalid/permission-requests"
_PERMISSION_REQUEST_POST_RE = re.compile(r"-X\s*POST|--request\s*POST", re.IGNORECASE)

_TK_OUTPUT_DECORATION_PATTERN = re.compile(
    r"Updated \S+ -> (?:open|in_progress|closed)|tk-step \S+ (?:title|summary): .*"
)

# A PURE tk lifecycle invocation: the command STARTS with the tk verb (`super` is the
# plugin-bypassing form). This is the HIDE rule -- a command that merely reaches a tk verb
# in a later segment (`cd /code && tk start x`) or wraps it in an assignment
# (`S1=$(tk create ...)`) still renders as work, so real work is never silently dropped.
# Contrast the truncation-exemption predicates (segment-wise, deliberately broader:
# over-preserving input is harmless, over-hiding is not).
_TK_COMMAND_PREFIX_RE = re.compile(r"^\s*(?:tk|ticket)\s+(?:super\s+)?(?:create|start|close)\b")


# The tk lifecycle verbs, in one place. Previously redefined in all four harnesses.
_TK_LIFECYCLE_VERBS: Final[frozenset[str]] = frozenset({"create", "start", "close"})


def is_pure_tk_lifecycle_command(command: str) -> bool:
    """True when ``command`` is nothing but a tk lifecycle invocation (rendered as a
    structural marker, not work). See ``_TK_COMMAND_PREFIX_RE`` for the rule.

    This is the HIDE rule. Contrast :func:`is_tk_lifecycle_anywhere`, which is deliberately
    broader; the two must not be collapsed.
    """
    return _TK_COMMAND_PREFIX_RE.match(command) is not None


def is_tk_lifecycle_anywhere(command: str) -> bool:
    """True when a tk lifecycle verb appears ANYWHERE in ``command``.

    This is the TK-COMMAND-STAMP rule, deliberately broader than the hide rule: a batched
    ``cd x && tk create --step ...`` still renders as work, but its command is stamped
    resident (``tk_command``) so the step progress view can read the plan out of it without
    fetching the input. Over-stamping is harmless; over-hiding work is not.

    Uses the shared shlex parser rather than a regex, so a ``tk close`` merely mentioned inside
    another command's quoted argument is not mistaken for a real lifecycle call.
    """
    parsed = parse_command(command)
    return parsed is not None and any(s.tk_verb in _TK_LIFECYCLE_VERBS for s in parsed.segments)


_PERMISSION_REQUEST_ID_KEY = '"request_id"'

# Ceiling on a preserved permission-request object. Preservation rescues the handful of
# fields the card renders; it is not licence to open an unbounded hole in the output
# limit. A body past this size is left to ordinary head truncation.
_MAX_PERMISSION_REQUEST_LENGTH = 8000

# Cap on the candidate `{`s probed in one tool result. Failing probes are not
# constant-time (each JSONDecodeError rescans up to the error position), so output that is
# mostly braces would otherwise cost O(braces x length). Legitimate output has only a
# handful of braces at or before the object's `request_id` key, so 1000 is two to three
# orders of magnitude of headroom.
_MAX_PERMISSION_REQUEST_PROBES = 1000

# Stateless and reused across candidate offsets rather than rebuilt per probe.
_JSON_DECODER = json.JSONDecoder()


def is_permission_request_call(raw_input: str) -> bool:
    """True when a tool call is an agent permission request: a POST to the reserved
    latchkey host. Detected from the tool INPUT alone, so a request is recognised the
    moment it is issued -- while it is still pending with no result yet, which is exactly
    when the user most needs to see and act on it."""
    return PERMISSION_REQUEST_HOST in raw_input and _PERMISSION_REQUEST_POST_RE.search(raw_input) is not None


def classify_tool_call_display(*, is_pure_tk: bool, raw_input: str) -> DisplayKind | None:
    """The render decision for one tool call, or ``None`` for an ordinary row: a pure tk
    lifecycle call is a hidden structural marker; a latchkey POST renders as the
    permission card. One helper so every parser stamps the same way."""
    if is_pure_tk:
        return DisplayKind.HIDDEN
    if is_permission_request_call(raw_input):
        return DisplayKind.PERMISSION_REQUEST
    return None


class PermissionRequest(FrozenModel):
    """A permission-request object found in a tool result."""

    details: dict[str, Any] = Field(description="The parsed permission-request object the gateway echoed")
    body: str = Field(description="The object's verbatim JSON text, exactly as it appeared in the tool output")


def _is_permission_request(parsed: dict[str, Any]) -> bool:
    """True for the shape the gateway echoes when it creates a request: a non-empty string
    `request_id` alongside a `payload` object. Demanding the pair keeps unrelated tool
    output that merely mentions a request id from being mistaken for a request."""
    request_id = parsed.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return False
    return isinstance(parsed.get("payload"), dict)


def find_permission_request(content: str) -> PermissionRequest | None:
    """Locate the permission-request object a creation POST echoed in ``content``.

    Returns the first such object, or None -- the case for essentially every tool result,
    so the cheap substring guard runs first. Candidate starts are the `{`s at or before
    the `request_id` key, each decoded with ``raw_decode`` (which reports where the value
    ended, making this robust to anything printed after the response). The probe count is
    capped so pathological brace-heavy output cannot stall parsing.
    """
    marker = content.find(_PERMISSION_REQUEST_ID_KEY)
    if marker < 0:
        return None
    probes = 0
    start = content.find("{")
    while 0 <= start <= marker:
        probes += 1
        if probes > _MAX_PERMISSION_REQUEST_PROBES:
            return None
        try:
            parsed, end = _JSON_DECODER.raw_decode(content, start)
        except json.JSONDecodeError:
            # A probe asking whether a JSON value begins at this `{` at all; "no" is the
            # ordinary answer for a brace in prose or shell output.
            parsed, end = None, start
        except RecursionError:
            # The C scanner recurses per nesting level on absurdly deep input; every later
            # candidate in the same nest would just recurse again, so give up on the
            # result: no gateway echo nests thousands deep.
            logger.warning("Giving up on a permission-request probe: absurdly deep JSON nesting in tool output")
            return None
        if isinstance(parsed, dict) and _is_permission_request(parsed):
            body = content[start:end]
            if len(body) > _MAX_PERMISSION_REQUEST_LENGTH:
                return None
            return PermissionRequest(details=parsed, body=body)
        start = content.find("{", start + 1)
    return None


# A token that looks like a tk step id (``cod-step-f1zl``). Lines carrying one are kept in
# the tk stamp even without the decoration shapes: the progress view's historical input
# fallback zips create titles positionally onto the step ids echoed anywhere in the
# output, so bare id echoes (a shell-var capture printing the id) must survive.
_STEP_ID_TOKEN_RE = re.compile(r"\S*-step-[a-z0-9]+")


def tk_stamp(content: str) -> str:
    """The tk-relevant lines of a tool output, as one resident string ('' = none).

    Exactly the lines the chat progress view reads back from the transcript: tk's
    decoration output plus any line carrying a step-id token. Stamped at parse time so the
    view never needs the raw output; capped as a guard against pathological output that
    happens to be full of step-id tokens.
    """
    if "-step-" not in content:
        return ""
    kept = [
        line
        for line in content.splitlines()
        if _TK_OUTPUT_DECORATION_PATTERN.search(line) or _STEP_ID_TOKEN_RE.search(line)
    ]
    if not kept:
        return ""
    joined = "\n".join(kept)
    return joined[:MAX_TK_STAMP_LENGTH]


def error_snippet(content: str) -> str:
    """The first non-empty line of a failed call's output, capped -- the resident glance.

    The full error text still loads on expand like any other output; this just keeps a
    failure recognisable in the collapsed row without a fetch.
    """
    for line in content.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:MAX_ERROR_SNIPPET_LENGTH]
    return ""
