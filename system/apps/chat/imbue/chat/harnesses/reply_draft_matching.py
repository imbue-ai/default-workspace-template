"""Whether a reply draft is one the transcript already holds, for every harness that offers drafts.

A draft stays on screen until the reply it previews lands in the transcript, and a source of
drafts keeps showing a reply for a moment after that (a terminal between turns, a live
connection until its turn ends), so each draft is compared with the latest replies the
transcript holds and dropped when it matches one. The comparison ignores everything but
letters and digits, and code-fence languages, since a draft read back off a terminal cannot
restore heading levels, fence languages, or list markers exactly.
"""

import re
from collections.abc import Sequence
from typing import Final

from imbue.imbue_common.pure import pure

# How much of a draft's start is looked for in the landed replies: long enough that two
# different replies rarely share it.
_COMPARED_PREFIX_CHARS: Final[int] = 80

_NON_ALPHANUMERIC_PATTERN: Final[re.Pattern[str]] = re.compile(r"[\W_]+")
# A code fence's info string (its language), which a terminal never shows.
_CODE_FENCE_INFO_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(\s*```)[^\n]*$", re.MULTILINE)


@pure
def comparison_key(text: str) -> str:
    """The text as the draft comparison sees it: its letters and digits, lowercased, without fence languages."""
    return _NON_ALPHANUMERIC_PATTERN.sub("", _CODE_FENCE_INFO_PATTERN.sub(r"\1", text)).lower()


@pure
def is_landed_reply(draft_text: str, landed_reply_texts: Sequence[str]) -> bool:
    """Whether ``draft_text`` is (the start of) one of the replies the transcript already holds.

    An empty draft counts as landed: there is nothing to show.
    """
    draft_key = comparison_key(draft_text)[:_COMPARED_PREFIX_CHARS]
    if draft_key == "":
        return True
    return any(draft_key in comparison_key(landed_text) for landed_text in landed_reply_texts)
