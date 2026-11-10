"""Reading and writing the agent's memory files: Claude's built-in memory, one memory per file.

Each memory is a markdown file in ``data/memories/`` with YAML frontmatter and a body::

    ---
    name: prefers-short-answers
    description: The user wants short, direct answers
    metadata:
      type: feedback
    ---

    Keep answers to a few sentences.

and ``MEMORY.md`` indexes them, one line each: ``- [Title](file.md) -- hook``. Only the few frontmatter keys the
page shows are read (``name``, ``description``, and ``type`` either under ``metadata`` or at the top level); every
other byte of a file is left as it was, so an edit changes only the description line and the body.
"""

import hashlib
import json
import re
from pathlib import Path
from typing import Final
from typing import NamedTuple

from about_me.data_types import Memory
from about_me.data_types import MemoryType

MEMORY_INDEX_NAME: Final[str] = "MEMORY.md"
# Files in the memory directory that are not memories: the index, and the README the template ships there.
NOT_MEMORIES: Final[frozenset[str]] = frozenset({MEMORY_INDEX_NAME, "README.md"})
_MEMORY_TYPES: Final[dict[str, MemoryType]] = {
    "user": "user",
    "feedback": "feedback",
    "project": "project",
    "reference": "reference",
}
_FENCE: Final[str] = "---"
_FILE_NAME: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.md$")
_INDEX_LINK: Final[re.Pattern[str]] = re.compile(r"\[(?P<title>[^\]]*)\]\((?P<file>[^)\s]+)\)")
_KEY_LINE: Final[re.Pattern[str]] = re.compile(r"^(?P<indent>\s*)(?P<key>[A-Za-z_]+):\s*(?P<value>.*)$")
# A value YAML would read as something other than plain text, so it is written double-quoted.
_NEEDS_QUOTES: Final[re.Pattern[str]] = re.compile(r"""(: |#|^[-?:,\[\]{}&*!|>'"%@`]|^\s|\s$)""")
_REVISION_LENGTH: Final[int] = 16


class ParsedMemory(NamedTuple):
    """A memory file's frontmatter lines, the keys the page reads from them, and its body."""

    frontmatter: list[str]
    name: str | None
    description: str | None
    memory_type: str | None
    body: str


def is_memory_file_name(name: str) -> bool:
    """Whether ``name`` is a memory file the page may read and write: a plain ``.md`` name, not the index or README."""
    return bool(_FILE_NAME.match(name)) and name not in NOT_MEMORIES


def revision(content: bytes) -> str:
    """A short hash of a file's bytes: any change to the file changes it."""
    return hashlib.sha256(content).hexdigest()[:_REVISION_LENGTH]


def _scalar(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1]
        return decoded if isinstance(decoded, str) else value
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


def yaml_scalar(value: str) -> str:
    """``value`` as a one-line YAML scalar: plain when that reads back the same, double-quoted otherwise."""
    return json.dumps(value, ensure_ascii=False) if _NEEDS_QUOTES.search(value) else value


def parse_memory(text: str) -> ParsedMemory:
    """The frontmatter and body of a memory file; a file with no frontmatter is all body."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != _FENCE:
        return ParsedMemory([], None, None, None, text)
    end = next((index for index in range(1, len(lines)) if lines[index].strip() == _FENCE), None)
    if end is None:
        return ParsedMemory([], None, None, None, text)
    frontmatter = lines[1:end]
    keys: dict[str, str] = {}
    nested_type: str | None = None
    for line in frontmatter:
        match = _KEY_LINE.match(line)
        if match is None:
            continue
        if match.group("indent"):
            if match.group("key") == "type":
                nested_type = _scalar(match.group("value"))
            continue
        keys[match.group("key")] = _scalar(match.group("value"))
    body = "\n".join(lines[end + 1 :]).strip("\n")
    return ParsedMemory(
        frontmatter=frontmatter,
        name=keys.get("name") or None,
        description=keys.get("description") or None,
        memory_type=nested_type or keys.get("type") or None,
        body=body,
    )


def render_memory(frontmatter: list[str], body: str) -> str:
    """A memory file from its frontmatter lines and body, in the layout the agent writes."""
    return f"{_FENCE}\n" + "".join(f"{line}\n" for line in frontmatter) + f"{_FENCE}\n\n{body.strip()}\n"


def with_description(frontmatter: list[str], description: str) -> list[str]:
    """``frontmatter`` with its top-level ``description`` replaced, or added after ``name`` when it has none."""
    line = f"description: {yaml_scalar(description)}"
    updated = list(frontmatter)
    for index, existing in enumerate(updated):
        match = _KEY_LINE.match(existing)
        if match is not None and not match.group("indent") and match.group("key") == "description":
            updated[index] = line
            return updated
    after_name = next(
        (index + 1 for index, existing in enumerate(updated) if existing.startswith("name:")),
        0,
    )
    return updated[:after_name] + [line] + updated[after_name:]


def new_user_memory(name: str, description: str, body: str) -> str:
    """A new ``user`` memory file, in the format the agent writes its own."""
    frontmatter = [
        f"name: {yaml_scalar(name)}",
        f"description: {yaml_scalar(description)}",
        "metadata:",
        "  type: user",
    ]
    return render_memory(frontmatter, body)


def index_titles(index: str | None) -> dict[str, str]:
    """Each memory file the index links to, with the title its line gives it."""
    if index is None:
        return {}
    titles: dict[str, str] = {}
    for line in index.split("\n"):
        match = _INDEX_LINK.search(line)
        if match is not None and match.group("file") not in titles:
            titles[match.group("file")] = match.group("title").strip()
    return titles


def index_line(title: str, file_name: str, hook: str) -> str:
    """One line of ``MEMORY.md`` pointing at ``file_name``."""
    return f"- [{title}]({file_name}) -- {hook}"


def without_index_lines(index: str, file_name: str) -> str:
    """``index`` without the lines that link to ``file_name``; every other line as it was."""
    target = f"]({file_name})"
    return "".join(line for line in index.splitlines(keepends=True) if target not in line)


def humanized(name: str) -> str:
    """A memory's slug or file stem as words: ``prefers-short-answers`` reads as ``Prefers short answers``."""
    words = re.sub(r"[-_]+", " ", name.removesuffix(".md")).strip()
    return words[:1].upper() + words[1:]


def memory_from_file(path: Path, content: bytes, titles: dict[str, str]) -> Memory:
    """What the page shows for one memory file."""
    parsed = parse_memory(content.decode("utf-8", errors="replace"))
    memory_type = _MEMORY_TYPES.get(parsed.memory_type or "", "other")
    return Memory(
        file_name=path.name,
        revision=revision(content),
        title=titles.get(path.name) or humanized(parsed.name or path.name),
        description=parsed.description,
        memory_type=memory_type,
        body=parsed.body,
    )
