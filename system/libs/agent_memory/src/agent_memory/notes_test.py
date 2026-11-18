from agent_memory.notes import index_titles
from agent_memory.notes import readable_text

NOTE = """---
name: likes-tea
description: User likes tea
metadata:
  node_type: memory
  type: user
  originSessionId: 842d7ff2
---

The user likes tea.

**Why:** Said so.
"""

INDEX = """# User Preferences

- [Likes tea](likes-tea.md) — user likes tea
- Always add emoticons
"""


def test_a_note_reads_as_its_summary_and_text_without_the_bookkeeping() -> None:
    text = readable_text("likes-tea.md", NOTE)
    assert text == "Summary: User likes tea\n\nThe user likes tea.\n\nWhy: Said so."
    assert "originSessionId" not in (text or "") and "name:" not in (text or "")


def test_the_index_reads_as_titles_and_summaries() -> None:
    assert readable_text("MEMORY.md", INDEX) == "User Preferences\n\nLikes tea — user likes tea\nAlways add emoticons"


def test_a_missing_file_stays_missing() -> None:
    assert readable_text("likes-tea.md", None) is None


def test_the_index_names_each_note_s_title() -> None:
    assert index_titles(INDEX) == {"likes-tea.md": "Likes tea"}
