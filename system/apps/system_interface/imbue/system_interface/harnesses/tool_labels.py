"""Human labels for a tool call, computed where the harness is already known.

Every tool call a parser emits carries two strings:

- ``header_label``  -- the tool's identity, for the transcript block header
- ``caption_label`` -- verb + target, for the live activity strip

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

GENERIC_CAPTION = "Running tool…"

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
