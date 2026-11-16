"""The dictation note: what the composer appends to a message that may hold dictated words, and how it is taken
back out of a person's words.

Speech recognition mishears, so a message sent from a draft that dictation wrote into ends with a short note, in
a tag pair, asking the model to read the words leniently. The note is for the model alone: the agent is handed it
and the transcript keeps it, while the tray's notification bubbles, which show the words to people, and the chat
namer, which feeds them to another prompt, read them through ``without_dictation_notes``.
"""

import re
from typing import Final

from imbue.imbue_common.pure import pure

# Cross-layer contract: the composer wraps the note in this tag pair and sets it off from the words by a blank
# line (``DICTATION_NOTE`` in frontend/src/models/dictationNote.ts, whose own pattern reads the same -- keep the
# two in step). Matched on the tags alone, so the note's wording can change without this pattern knowing, and
# non-greedy, since a text that joins several messages carries one note per dictated message.
_DICTATION_NOTE_RE: Final[re.Pattern[str]] = re.compile(r"(?:\n\n)?<dictation-note>[\s\S]*?</dictation-note>")


@pure
def without_dictation_notes(text: str) -> str:
    """``text`` without any dictation note the composer appended, nor the blank line that set each off."""
    return _DICTATION_NOTE_RE.sub("", text)
