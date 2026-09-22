"""Human labels for a tool call, computed where the harness is already known.

Every tool call a parser emits carries:

- ``header_label``  -- the tool's identity, for the transcript block header
- ``caption_label`` -- verb + target, for the live activity strip
- ``action_verb`` / ``action_target`` -- what the call DID, for the transcript's
  inline tool chips: a past-tense verb and the thing it acted on, which for a
  file is its NAME rather than its path (the chip is a phrase to read, and a
  path in the middle of one is noise; the whole path is a click away in the
  chip's panel). Kept as two fields rather than one joined string because
  splitting a joined label back would have to guess where a multi-word verb like
  "loaded skill" ends.
- ``action_note`` -- the agent's OWN words for why it made the call, when the tool
  records them. Only claude's shell and delegation tools take one, so this is
  absent far more often than not, and a missing note is rendered as nothing
  rather than guessed at.

They are computed HERE, in the harness's own parser, rather than in the frontend.
The frontend renders whichever it needs and so has to know nothing about which
harness produced the event -- which matters most for codex, where code mode names
every operation ``exec`` and buries the real one in a JavaScript argument.

The two strings differ for claude (``Tool: Read`` / ``Reading foo.py``) and are
usually equal for codex, whose header would otherwise read a useless ``Tool: exec``.

This module holds only the pieces both harnesses share; the per-harness tables
live in :mod:`tool_labels` and :mod:`tool_labels`.
"""

import json
import re
from typing import Any

from imbue.imbue_common.pure import pure

# Targets are appended to a verb in a narrow strip, so they are truncated well
# before the strip would wrap.
MAX_TARGET_LENGTH = 60

# A note is the agent's own sentence about a call, shown on a chip in a wrapping
# row. The cap is generous -- the tools that take one ask for a handful of words,
# so it is a guard against a runaway description rather than a routine trim.
MAX_NOTE_LENGTH = 80

GENERIC_CAPTION = "Running tool…"

# The input key an agent states its reason in. Claude's Bash and Agent tools both
# use ``description``; no other harness's tools record one at all.
NOTE_INPUT_KEY = "description"

_MCP_PREFIX = "mcp__"
_MCP_SEPARATOR = "__"


@pure
def basename(path: str) -> str:
    """The final path segment, or the whole string when there is no separator."""
    return path.rstrip("/").rsplit("/", 1)[-1] or path


@pure
def shorten(text: str, max_length: int = MAX_TARGET_LENGTH) -> str:
    """Collapse whitespace and clip to ``max_length``, marking the clip with an ellipsis."""
    collapsed = re.sub(r"\s+", " ", text).strip()
    if len(collapsed) <= max_length:
        return collapsed
    return collapsed[: max_length - 1] + "…"


@pure
def quoted(text: str) -> str:
    """A search term as it should read in a caption: shortened and in quotes."""
    return f'"{shorten(text)}"'


@pure
def mcp_caption(tool_name: str) -> str | None:
    """``mcp__<server>__<tool>`` -> ``Running <tool with spaces>``; None for non-MCP names.

    Splits on the LAST separator because a server name may itself contain one
    (both harnesses sanitise dots to underscores, so ``server.one`` arrives as
    ``server_one`` but a hand-named server can still be ``a__b``).
    """
    if not tool_name.startswith(_MCP_PREFIX):
        return None
    separator_index = tool_name.rfind(_MCP_SEPARATOR)
    if separator_index <= len(_MCP_PREFIX) - 1:
        return None
    tool_part = tool_name[separator_index + len(_MCP_SEPARATOR) :]
    if not tool_part:
        return None
    return f"Running {tool_part.replace('_', ' ')}"


# The past tense of every caption verb the harnesses use, in one table because
# they deliberately share that vocabulary (see the noun/verb tables in each
# harness's own module, which are written to read alike). A chip describes work
# that already happened, so it needs the past tense; lowercase so the eye goes to
# the target beside it, which is the half that identifies the call.
_PAST_TENSE_BY_PARTICIPLE: dict[str, str] = {
    "Reading": "read",
    "Writing": "wrote",
    "Editing": "edited",
    "Editing notebook": "edited notebook",
    "Running": "ran",
    "Searching": "searched",
    "Searching the web": "searched the web",
    "Listing": "listed",
    "Fetching page": "fetched",
    "Loading skill": "loaded skill",
    "Loading tool": "loaded tool",
    "Querying language server": "queried language server",
    "Monitoring": "monitored",
    "Sending message": "sent",
    "Checking sources": "checked sources",
    "Retrieving results": "retrieved results",
}


@pure
def past_tense(participle: str) -> str:
    """A caption's present participle as the past tense a chip needs.

    Falls back to lowercasing the participle unchanged. That reads oddly for a
    verb this table has not learned yet ("listing foo" rather than "listed foo"),
    which is the point: it stays legible instead of dropping the verb, and the
    oddity is what gets the missing entry noticed.
    """
    known = _PAST_TENSE_BY_PARTICIPLE.get(participle)
    if known is not None:
        return known
    return participle[:1].lower() + participle[1:]


@pure
def stated_note(tool_input: dict[str, Any]) -> str | None:
    """The agent's own words for this call, or None when the tool records none.

    Deliberately never inferred. A tool whose input has no note field -- every
    file read, edit and write -- yields None, and the chip then says what the call
    did instead. A guessed reason would be worse than none.
    """
    value = tool_input.get(NOTE_INPUT_KEY)
    if not isinstance(value, str) or not value.strip():
        return None
    return shorten(value, MAX_NOTE_LENGTH)


@pure
def parse_input_preview(input_preview: str) -> dict[str, Any]:
    """The tool input as a dict, or empty when it is absent, not JSON, or not an object.

    Some harness inputs are not JSON objects at all (codex's code-mode JS program,
    a bare string argument). That is expected, not exceptional: the caller falls
    back to a generic label rather than guessing at an unparseable input.
    """
    try:
        parsed = json.loads(input_preview)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


@pure
def first_string_value(source: dict[str, Any], *keys: str) -> str | None:
    """The first key present with a non-empty string value, in the order given."""
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value:
            return value
    return None
