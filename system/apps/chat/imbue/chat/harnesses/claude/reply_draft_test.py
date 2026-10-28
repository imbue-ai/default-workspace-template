from pathlib import Path

from imbue.chat.harnesses.claude.reply_draft import ClaudeReplyDraft
from imbue.chat.harnesses.claude.reply_draft import is_landed_reply
from imbue.chat.harnesses.claude.reply_draft import pane_width_of
from imbue.chat.harnesses.claude.reply_draft import unwrap_soft_wrapped_rows

# Real captures (``tmux capture-pane -e -J -p``) of one Claude Code 2.1 agent answering a prompt
# on a 200-column pane, and the replies its session file recorded.
_FIXTURE_DIR = Path(__file__).parent / "testdata" / "reply_draft"


def _fixture(name: str) -> str:
    return (_FIXTURE_DIR / name).read_text()


def test_a_reply_still_streaming_is_read_as_its_paragraphs_so_far() -> None:
    draft = ClaudeReplyDraft()

    text = draft.read(_fixture("pane_reply_streaming.txt"), [_fixture("landed_previous_reply.md")])

    assert text.startswith("**How Tides Work**\n\nTides are the regular rise and fall of the sea")
    assert text.endswith("so a second bulge forms there.")
    # The rows the TUI wrapped at the pane's width are one paragraph again.
    assert "facing it, less on Earth's center" in text


def test_a_finished_reply_reads_as_the_reply_its_transcript_records_but_for_the_heading_level() -> None:
    draft = ClaudeReplyDraft()

    text = draft.read(_fixture("pane_reply_finished.txt"), [_fixture("landed_previous_reply.md")])

    assert text == _fixture("landed_reply.md").replace("## How Tides Work", "**How Tides Work**")


def test_the_previous_reply_on_screen_before_the_new_one_starts_is_no_draft() -> None:
    draft = ClaudeReplyDraft()

    assert draft.read(_fixture("pane_previous_reply_only.txt"), [_fixture("landed_previous_reply.md")]) == ""


def test_the_finished_reply_is_no_draft_once_the_transcript_holds_it() -> None:
    draft = ClaudeReplyDraft()
    landed = [_fixture("landed_previous_reply.md"), _fixture("landed_reply.md")]

    assert draft.read(_fixture("pane_reply_finished.txt"), landed) == ""


def test_a_reset_draft_forgets_the_reply_it_stitched() -> None:
    draft = ClaudeReplyDraft()
    draft.read(_fixture("pane_reply_streaming.txt"), [])

    draft.reset()

    assert draft.read("", []) == ""


def test_the_pane_width_is_the_width_tmux_padded_the_rows_to() -> None:
    assert pane_width_of(_fixture("pane_reply_finished.txt")) == 200
    assert pane_width_of("") == 0


def test_is_landed_reply_ignores_the_markdown_the_reverse_mapping_cannot_restore() -> None:
    landed = ["## Steps\n\n```python\nprint('hi')\n```\n\n1. First"]

    assert is_landed_reply("**Steps**\n\n```\nprint('hi')\n```", landed)
    assert not is_landed_reply("A different reply entirely.", landed)
    assert is_landed_reply("   ", landed)


def test_unwrap_joins_only_rows_whose_next_word_would_not_have_fit() -> None:
    # Width 20: a row plus its 2-column indent, a space, and the next word.
    rows = [
        "one two three four",
        "five six",
        "short",
        "next row",
    ]

    assert unwrap_soft_wrapped_rows(rows, 20) == ["one two three four five six", "short", "next row"]


def test_unwrap_leaves_list_items_tables_and_code_rows_alone() -> None:
    rows = [
        "- item one is long",
        "- item two",
        "| a long table cell |",
        "| b |",
        "```",
        "code row is long xx",
        "continued",
        "```",
    ]

    assert unwrap_soft_wrapped_rows(rows, 20) == rows


def test_unwrap_joins_a_list_items_wrapped_continuation() -> None:
    assert unwrap_soft_wrapped_rows(["- **an item** that", "  wraps over"], 18) == ["- **an item** that wraps over"]
