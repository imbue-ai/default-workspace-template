"""Reading the lookup's profile memory: which of its lines are facts, and what each one says.

The ``personalized-ideas`` skill writes ``user-public-profile.md`` as a Claude memory file: YAML frontmatter, then
one fact per line in a fixed shape (``.agents/skills/personalized-ideas/SKILL.md``, step 5)::

    - <fact> ([<source title>](<url>)) <!-- found YYYY-MM-DD -->
    - <fact the user shared> (you told me) <!-- found YYYY-MM-DD -->

and a last plain line saying the facts came from a lookup the user asked for. Every body line that starts with
``- `` is a fact; one not in either shape is still a fact, shown as written and deleted whole. Everything works on
the file's bytes, so a deletion leaves every other byte as it was.
"""

import hashlib
import re
from typing import Final
from typing import NamedTuple

from about_me.data_types import Fact

FACT_PREFIX: Final[bytes] = b"- "
_FRONTMATTER_FENCE: Final[bytes] = b"---"
_WEB_FACT: Final[re.Pattern[str]] = re.compile(
    r"^- (?P<text>.+?) \(\[(?P<title>[^\]]*)\]\((?P<url>\S+?)\)\)\s*<!-- found (?P<date>\d{4}-\d{2}-\d{2}) -->\s*$"
)
_TOLD_FACT: Final[re.Pattern[str]] = re.compile(
    r"^- (?P<text>.+?) \(you told me\)\s*<!-- found (?P<date>\d{4}-\d{2}-\d{2}) -->\s*$"
)
# The target of every markdown link on a line: what a line cites, in the shape or not.
_MARKDOWN_LINK_TARGET: Final[re.Pattern[str]] = re.compile(r"\]\((\S+?)\)")
_LINKABLE_SCHEMES: Final[tuple[str, ...]] = ("http://", "https://")
_LINE_HASH_LENGTH: Final[int] = 16


class ProfileLine(NamedTuple):
    """One fact line of the profile: where it is in the file, its exact bytes, and what it says."""

    index: int
    raw: bytes
    fact: Fact


def fact_id(index: int, raw: bytes) -> str:
    """The id of the line at ``index`` holding exactly ``raw``: any edit to the file above or on it changes it."""
    return f"{index}-{hashlib.sha256(raw).hexdigest()[:_LINE_HASH_LENGTH]}"


def _body_start(lines: list[bytes]) -> int:
    """The index of the first line after the frontmatter; 0 for a file with none, 1 for an unclosed one."""
    if not lines or lines[0].strip() != _FRONTMATTER_FENCE:
        return 0
    for index in range(1, len(lines)):
        if lines[index].strip() == _FRONTMATTER_FENCE:
            return index + 1
    return 1


def _decode_line(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace").rstrip("\r\n")


def parse_fact_line(index: int, raw: bytes) -> Fact:
    """The fact one ``- `` line holds; a line in neither shape (or citing a link that is not http(s)) is kept whole."""
    line = _decode_line(raw)
    identifier = fact_id(index, raw)
    web = _WEB_FACT.match(line)
    if web is not None and web.group("url").startswith(_LINKABLE_SCHEMES):
        return Fact(
            id=identifier,
            text=web.group("text"),
            source_title=web.group("title"),
            source_url=web.group("url"),
            is_told_by_user=False,
            found_on=web.group("date"),
            is_parsed=True,
        )
    told = _TOLD_FACT.match(line)
    if told is not None:
        return Fact(
            id=identifier,
            text=told.group("text"),
            source_title=None,
            source_url=None,
            is_told_by_user=True,
            found_on=told.group("date"),
            is_parsed=True,
        )
    return Fact(
        id=identifier,
        text=line.removeprefix("- ").strip(),
        source_title=None,
        source_url=None,
        is_told_by_user=False,
        found_on=None,
        is_parsed=False,
    )


def split_lines(content: bytes) -> list[bytes]:
    """The file's lines with their line endings, so joining them gives back the same bytes."""
    return content.splitlines(keepends=True)


def parse_profile(content: bytes) -> tuple[ProfileLine, ...]:
    """Every fact line of the profile's body, in file order."""
    lines = split_lines(content)
    return tuple(
        ProfileLine(index=index, raw=lines[index], fact=parse_fact_line(index, lines[index]))
        for index in range(_body_start(lines), len(lines))
        if lines[index].startswith(FACT_PREFIX)
    )


def cited_urls(lines: tuple[ProfileLine, ...]) -> frozenset[str]:
    """Every link target the lines cite, whether or not a line is in the fixed shape."""
    return frozenset(url for line in lines for url in _MARKDOWN_LINK_TARGET.findall(_decode_line(line.raw)))
