"""The reply Claude is writing, read off its terminal before its transcript holds it.

Claude Code appends an assistant text block to the session JSONL only once the block is
finished, while its TUI shows the text as it streams. On a reply of a few hundred words
that is several seconds in which the agent's terminal shows the answer and the chat shows
nothing. mngr's ``stream_snapshot`` already turns a captured pane back into markdown and
stitches successive captures of a reply that scrolls past the top of the pane; this module
keeps that stitching state for one agent and decides whether what it read is worth showing.

The TUI breaks a paragraph into rows the width of the pane, and the chat renders every newline
as a line break, so a row that ends only because the next word did not fit is joined back to
the row after it: the word-wrap rule that made it is exactly that the next row's first word
would have overflowed it. Rows the markdown itself breaks (list items, headings, quotes,
tables, code) are left alone.

What the pane shows is a draft only while it is not a reply the transcript already holds:
between turns, while a tool runs, and in the moment after a block lands, the last
assistant block on screen is one the chat has, so it is compared with the latest replies
of the transcript and dropped when it matches one (``reply_draft_matching``).
"""

import re
from collections.abc import Sequence
from typing import Final

from pydantic import PrivateAttr

from imbue.chat.harnesses.reply_draft_matching import is_landed_reply
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mngr_claude.resources.stream_snapshot import StreamBufferState
from imbue.mngr_claude.resources.stream_snapshot import convert_block_to_markdown
from imbue.mngr_claude.resources.stream_snapshot import extract_message_region
from imbue.mngr_claude.resources.stream_snapshot import strip_ansi

# The indent the TUI draws every row of an assistant block with (after the marker, on the first).
_BLOCK_INDENT_WIDTH: Final[int] = 2

# A row that starts one of these begins a markdown element of its own, never a wrapped row.
_BLOCK_START_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\s*(?:[-*+] |\d+[.)] |#|>|\||```)")
_CODE_FENCE: Final[str] = "```"
# The markdown the reverse mapping adds around a run, which the pane did not draw.
_MARKDOWN_LINK_PATTERN: Final[re.Pattern[str]] = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MARKDOWN_EMPHASIS_PATTERN: Final[re.Pattern[str]] = re.compile(r"\*\*|`|\*")


@pure
def pane_width_of(pane_text: str) -> int:
    """The width of the captured pane: tmux pads every drawn row of it to the full width."""
    return max((len(strip_ansi(row)) for row in pane_text.split("\n")), default=0)


@pure
def _drawn_width(markdown_row: str) -> int:
    """How many columns a converted row took on screen, before its markdown was added back."""
    unlinked = _MARKDOWN_LINK_PATTERN.sub(r"\1", markdown_row)
    return _BLOCK_INDENT_WIDTH + len(_MARKDOWN_EMPHASIS_PATTERN.sub("", unlinked))


@pure
def _is_soft_wrapped(row: str, next_row: str, pane_width: int) -> bool:
    """Whether ``row`` ended only because the first word of ``next_row`` would not fit beside it."""
    if row.strip() == "" or next_row.strip() == "" or _BLOCK_START_PATTERN.match(next_row):
        return False
    next_word = _MARKDOWN_EMPHASIS_PATTERN.sub("", next_row.split()[0])
    return _drawn_width(row) + 1 + len(next_word) > pane_width


@pure
def unwrap_soft_wrapped_rows(rows: Sequence[str], pane_width: int) -> list[str]:
    """Join each row the TUI wrapped at the pane's width onto the row before it, outside code blocks."""
    joined: list[str] = []
    is_in_code_block = False
    is_previous_wrapped = False
    for index, row in enumerate(rows):
        if is_previous_wrapped:
            joined[-1] = f"{joined[-1]} {row.strip()}"
        else:
            joined.append(row)
        if row.lstrip().startswith(_CODE_FENCE):
            is_in_code_block = not is_in_code_block
        next_row = rows[index + 1] if index + 1 < len(rows) else ""
        is_previous_wrapped = (
            not is_in_code_block
            and not row.lstrip().startswith(("|", _CODE_FENCE))
            and _is_soft_wrapped(row, next_row, pane_width)
        )
    return joined


class ClaudeReplyDraft(MutableModel):
    """The stitched in-progress reply of one Claude agent, fed one pane capture at a time.

    Not thread-safe: one poller owns it.
    """

    model_config = {"arbitrary_types_allowed": True}

    _buffer: StreamBufferState = PrivateAttr(default_factory=StreamBufferState)

    def reset(self) -> None:
        """Forget the reply read so far (the turn ended)."""
        self._buffer.reset()

    def read(self, pane_text: str, landed_reply_texts: Sequence[str]) -> str:
        """Fold one pane capture in and return the draft to show, or "" when there is none."""
        region = extract_message_region(pane_text)
        if region is not None:
            region_lines, has_marker = region
            self._buffer.ingest_block(convert_block_to_markdown(region_lines), has_marker)
        draft_text = "\n".join(unwrap_soft_wrapped_rows(self._buffer.body_lines, pane_width_of(pane_text))).strip()
        if is_landed_reply(draft_text, landed_reply_texts):
            return ""
        return draft_text
