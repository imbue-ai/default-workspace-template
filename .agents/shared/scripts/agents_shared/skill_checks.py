"""The checks the skill authoring guide sets on a skill's files.

Five checks, each over one skill (``check_failures``):

- ``size``: SKILL.md is at most 14,000 bytes and its body at most 500 lines.
- ``reachability``: every file in the skill's directory is linked from SKILL.md or from a file
  SKILL.md reaches.
- ``depth``: every markdown file the skill reaches, in its own directory or under
  ``.agents/shared/``, is at most two links from SKILL.md.
- ``paths``: every path its markdown names under ``.agents/``, ``system/`` or ``docs/`` exists.
- ``contents``: a SKILL.md over 200 lines, or a reference over 100, has a list in its first 100
  lines naming every H2 section below it.

And one over the layer the skills share (``unreached_shared_files``).

A link is a path written in a markdown file that resolves to a file: from the repository root,
from the linking file's directory or its ``references/``, or, in one of the skill's own files,
from the skill's directory or its ``references/``. A script of the skill that imports a package
of the skill's uv project reaches the project. A path holding a placeholder (``<name>``,
``$VAR``, a glob) links to nothing.
The crawl follows links through the skill's own markdown and through ``.agents/shared/``; a
file in another skill is a link's end, not a place to read on from.

``docs/system/skill_authoring_guide.md`` holds the rules and the reasons for them.
"""

from __future__ import annotations

import enum
import os
import posixpath
import re
import subprocess
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

GUIDE_PATH = "docs/system/skill_authoring_guide.md"

MAX_SKILL_MD_BYTES = 14_000
MAX_BODY_LINES = 500
_MAX_DEPTH = 2
_CONTENTS_SKILL_MD_BODY_LINES = 200
_CONTENTS_REFERENCE_LINES = 100
_CONTENTS_WITHIN_LINES = 100

_SHARED_DIR = ".agents/shared"
_SKILLS_DIR = ".agents/skills"
# The prefixes a path must exist under to be checked. Everything else a skill names (``data/``,
# a home directory, another machine) is outside what the repository carries.
_TRACKED_PREFIXES = (".agents/", "system/", "docs/")

# A maximal run of the characters a path is written in. One class, so the scan is linear in
# the text however long a run is.
_PATH_RUN = re.compile(r"[\w.~<>{}$*@+/-]+")
_PLACEHOLDER = re.compile(r"[<>{}$*]|\.\.\.")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?(.+)$")
_MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")
_PRUNED_DIR_NAMES = frozenset({".git", ".venv", "__pycache__", "node_modules"})


class Check(enum.StrEnum):
    SIZE = "size"
    REACHABILITY = "reachability"
    DEPTH = "depth"
    PATHS = "paths"
    CONTENTS = "contents"


@dataclass(frozen=True)
class CheckFailure:
    check: Check
    detail: str


_GOALS: dict[Check, str] = {
    Check.SIZE: (
        "After a compaction an agent keeps only the first 5,000 tokens of a skill it invoked, "
        "about 14 KB. Keep the standing instructions and the step list in SKILL.md, and move "
        "each step's detail into a reference the step names."
    ),
    Check.REACHABILITY: (
        "Every file in a skill's directory is one an agent can be sent to. Link each of these "
        "from SKILL.md or from a reference SKILL.md links, or delete it."
    ),
    Check.DEPTH: (
        "An agent reaches any file within two links of SKILL.md. Name each of these from "
        "SKILL.md or from the first reference in its chain, or fold it into the file that "
        "links it."
    ),
    Check.PATHS: (
        "A path an agent is given exists. Correct each of these; write a path outside this "
        "repository, or an example name, with a placeholder (`<mngr>/apps/minds/`, "
        "`system/apps/<name>/`)."
    ),
    Check.CONTENTS: (
        "A long file has a list in its first 100 lines naming every H2 section below it (in a "
        "SKILL.md, the step list), so an agent finds one section without reading the file."
    ),
}
_PERMISSION = (
    "Restructuring the skill is permitted: split, move and rewrite its files as far as that "
    f"takes. The rules and their reasons are in {GUIDE_PATH}."
)


def describe_failures(failures: Iterable[CheckFailure]) -> str:
    """The failures grouped by check, each under the goal it serves, then what is permitted."""
    by_check: dict[Check, list[str]] = {}
    for failure in failures:
        by_check.setdefault(failure.check, []).append(failure.detail)
    lines: list[str] = []
    for check in Check:
        if check in by_check:
            lines.append(f"[{check.value}] {_GOALS[check]}")
            lines.extend(f"  - {detail}" for detail in by_check[check])
    lines.append(_PERMISSION)
    return "\n".join(lines)


def is_test_file(path: Path) -> bool:
    return (
        path.name.endswith("_test.py")
        or path.name.startswith("test_")
        or path.name == "conftest.py"
    )


class SkillTree:
    """A repository's files as the checks see them, by path relative to its root.

    In a git checkout the files are the ones git tracks or would track. A generated or ignored
    file is on one machine and not on another, and a check must not pass on one and fail on
    the other. Outside a checkout (a test's fixture) they are what is on disk.

    File contents are cached, so make a new tree (or call ``forget``) after writing a file.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._is_git = (self.root / ".git").exists()
        self._git_files: frozenset[str] | None = None
        self._git_dirs: frozenset[str] | None = None
        self._text: dict[str, str] = {}
        self._links: dict[tuple[str, str | None], frozenset[str]] = {}

    def relative(self, path: Path) -> str:
        """``path`` relative to the root, in POSIX form; raises ``ValueError`` outside it."""
        return path.resolve().relative_to(self.root).as_posix()

    def forget(self, path: Path) -> None:
        relative = self.relative(path)
        self._text.pop(relative, None)
        for key in [key for key in self._links if key[0] == relative]:
            del self._links[key]

    def _listed(self) -> tuple[frozenset[str], frozenset[str]]:
        if self._git_files is None or self._git_dirs is None:
            listing = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.root),
                    "ls-files",
                    "-z",
                    "--cached",
                    "--others",
                    "--exclude-standard",
                ],
                capture_output=True,
                check=True,
            ).stdout.decode("utf-8")
            files = frozenset(
                name
                for name in listing.split("\0")
                if name and os.path.lexists(self.root / name)
            )
            dirs: set[str] = set()
            for name in files:
                parent = posixpath.dirname(name)
                while parent and parent not in dirs:
                    dirs.add(parent)
                    parent = posixpath.dirname(parent)
            self._git_files, self._git_dirs = files, frozenset(dirs)
        return self._git_files, self._git_dirs

    def is_file(self, relative: str) -> bool:
        if self._is_git:
            return relative in self._listed()[0]
        return (self.root / relative).is_file()

    def is_dir(self, relative: str) -> bool:
        if self._is_git:
            return relative in self._listed()[1]
        return (self.root / relative).is_dir()

    def files_under(self, directory: str) -> list[str]:
        if self._is_git:
            prefix = directory.rstrip("/") + "/"
            return sorted(name for name in self._listed()[0] if name.startswith(prefix))
        found: list[str] = []
        for parent, dir_names, file_names in os.walk(self.root / directory):
            dir_names[:] = [name for name in dir_names if name not in _PRUNED_DIR_NAMES]
            found.extend(
                (Path(parent) / name).relative_to(self.root).as_posix()
                for name in file_names
            )
        return sorted(found)

    def ignored(self, relatives: Iterable[str]) -> set[str]:
        """Which of these paths git ignores. None, outside a checkout."""
        candidates = sorted(set(relatives))
        if not self._is_git or not candidates:
            return set()
        batch = subprocess.run(
            ["git", "-C", str(self.root), "check-ignore", "-z", "--stdin"],
            input="\0".join(candidates).encode("utf-8"),
            capture_output=True,
            check=False,
        )
        if batch.returncode in (0, 1):
            return {name for name in batch.stdout.decode("utf-8").split("\0") if name}
        # One path git refuses to judge (it lies beyond a symbolic link, say) fails the whole
        # batch, so ask about each on its own and count a refusal as not ignored.
        return {
            name
            for name in candidates
            if subprocess.run(
                ["git", "-C", str(self.root), "check-ignore", "-q", name],
                capture_output=True,
                check=False,
            ).returncode
            == 0
        }

    def text(self, relative: str) -> str:
        if relative not in self._text:
            self._text[relative] = (self.root / relative).read_text(encoding="utf-8")
        return self._text[relative]

    def _node(self, relative: str) -> str:
        """A file inside a uv project stands for the project: its modules are code, reached
        by imports, and the checks ask only that the project is named."""
        parent = posixpath.dirname(relative)
        while parent:
            if self.is_file(f"{parent}/pyproject.toml"):
                return parent
            parent = posixpath.dirname(parent)
        return relative

    def links(self, markdown: str, skill: str | None) -> frozenset[str]:
        """The files ``markdown`` links, as nodes (see ``_node``)."""
        key = (markdown, skill)
        if key not in self._links:
            file_dir = posixpath.dirname(markdown)
            # A shared file's relative path is not about whichever skill is reading it.
            is_own = skill is not None and markdown.startswith(f"{skill}/")
            bases = [
                "",
                file_dir,
                posixpath.join(file_dir, "references"),
                *([skill, posixpath.join(skill, "references")] if is_own else []),
            ]
            found: set[str] = set()
            for token in _path_tokens(self.text(markdown)):
                for base in bases:
                    target = posixpath.normpath(posixpath.join(base, token))
                    if target.startswith(".."):
                        continue
                    if self.is_file(target):
                        found.add(self._node(target))
                        break
                    if self.is_dir(target):
                        project = self._node(f"{target}/.")
                        if project != f"{target}/.":
                            found.add(project)
                            break
            found.discard(markdown)
            self._links[key] = frozenset(found)
        return self._links[key]

    def own_nodes(self, directory: str) -> set[str]:
        """The files of a directory that must be reached: not its hidden files or its tests."""
        return {
            self._node(name)
            for name in self.files_under(directory)
            if not any(
                part.startswith(".") for part in name[len(directory) :].split("/")
            )
            and not is_test_file(Path(name))
        }


def _path_tokens(text: str) -> Iterator[str]:
    """Every path-shaped run of text that names a concrete, repository-relative path."""
    for match in _PATH_RUN.finditer(text):
        run = match.group(0)
        if run.startswith(("/", "~")) or _PLACEHOLDER.search(run):
            continue
        token = run.rstrip(".")
        # A path has a directory in it, or is a file name with an extension.
        if "/" in token.strip("/") or "." in token.strip("."):
            yield token


def _is_crawled(node: str, skill: str | None) -> bool:
    """Whether the crawl reads on from ``node``: markdown of the skill's own or the shared
    layer's. A skill's ``assets/`` are templates and samples, not instructions."""
    if not node.endswith(".md"):
        return False
    if node.startswith(_SHARED_DIR + "/"):
        return True
    return (
        skill is not None
        and node.startswith(skill + "/")
        and not node.startswith(skill + "/assets/")
    )


@dataclass(frozen=True)
class _Crawl:
    depth: dict[str, int]
    parent: dict[str, str]

    def chain(self, node: str) -> str:
        steps = [node]
        while steps[-1] in self.parent:
            steps.append(self.parent[steps[-1]])
        return " -> ".join(reversed(steps))


def _crawl(tree: SkillTree, start: str, skill: str | None) -> _Crawl:
    depth = {start: 0}
    parent: dict[str, str] = {}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        if current != start and not (
            _is_crawled(current, skill) and tree.is_file(current)
        ):
            continue
        for linked in sorted(tree.links(current, skill)):
            if linked not in depth:
                depth[linked] = depth[current] + 1
                parent[linked] = current
                queue.append(linked)
    return _Crawl(depth=depth, parent=parent)


_IMPORTED_NAME = re.compile(r"^\s*(?:from|import)\s+(\w+)", re.MULTILINE)


def _imported_projects(tree: SkillTree, skill: str, reached: Iterable[str]) -> set[str]:
    """The skill's uv projects that one of its reached scripts imports a package of."""
    projects = {
        node for node in tree.own_nodes(skill) if tree.is_file(f"{node}/pyproject.toml")
    }
    scripts = [
        node
        for node in reached
        if node.endswith(".py") and node.startswith(f"{skill}/")
    ]
    return {
        project
        for script in scripts
        for name in _IMPORTED_NAME.findall(tree.text(script))
        for project in projects
        if tree.is_file(f"{project}/{name}/__init__.py")
    }


def _checked_markdown(crawl: _Crawl, skill: str, tree: SkillTree) -> list[str]:
    return sorted(
        node for node in crawl.depth if _is_crawled(node, skill) and tree.is_file(node)
    )


def _size_failures(tree: SkillTree, skill_md: str) -> list[CheckFailure]:
    failures: list[CheckFailure] = []
    size = len(tree.text(skill_md).encode("utf-8"))
    if size > MAX_SKILL_MD_BYTES:
        failures.append(
            CheckFailure(
                Check.SIZE,
                f"SKILL.md is {size:,} bytes; the most is {MAX_SKILL_MD_BYTES:,}",
            )
        )
    body_lines = len(_body_lines(tree.text(skill_md)))
    if body_lines > MAX_BODY_LINES:
        failures.append(
            CheckFailure(
                Check.SIZE,
                f"SKILL.md body is {body_lines} lines; the most is {MAX_BODY_LINES}",
            )
        )
    return failures


def _body_lines(skill_md_text: str) -> list[str]:
    """The lines after the frontmatter, or every line when there is none to find."""
    lines = skill_md_text.splitlines()
    if lines and lines[0].strip() == "---" and "---" in lines[1:]:
        return lines[lines.index("---", 1) + 1 :]
    return lines


def _missing_paths(tree: SkillTree, markdown: str) -> list[str]:
    named = {
        posixpath.normpath(token)
        for token in _path_tokens(tree.text(markdown))
        if token.startswith(_TRACKED_PREFIXES)
    }
    absent = {path for path in named if not (tree.is_file(path) or tree.is_dir(path))}
    return sorted(absent - tree.ignored(absent))


@dataclass
class _List:
    start: int
    end: int
    items: list[str] = field(default_factory=list)


def _outline(text: str) -> tuple[list[tuple[int, str]], list[_List]]:
    """A file's H2 headings and its lists, each with the lines it spans.

    A list is a run of items broken only by blank lines and indented continuations. Items
    inside a fenced block count, as one list per block: a copyable checklist is a contents
    list. A heading inside a fenced block is not a section.
    """
    headings: list[tuple[int, str]] = []
    lists: list[_List] = []
    current: _List | None = None
    fence: str | None = None
    for index, line in enumerate(text.splitlines()):
        fence_match = _FENCE.match(line)
        if fence_match is not None:
            marker, rest = fence_match.groups()
            if fence is None:
                fence, current = marker, None
                continue
            if marker[0] == fence[0] and len(marker) >= len(fence) and not rest.strip():
                fence, current = None, None
                continue
        item = _LIST_ITEM.match(line)
        if item is not None:
            if current is None:
                current = _List(start=index, end=index)
                lists.append(current)
            current.items.append(item.group(1))
            current.end = index
        elif not line.strip():
            continue
        elif current is not None and line[0] in " \t":
            current.items[-1] += " " + line.strip()
            current.end = index
        else:
            current = None
            if fence is None and line.startswith("## "):
                headings.append((index, line[3:].strip()))
    return headings, lists


def _plain(markdown: str) -> str:
    text = _MARKDOWN_LINK.sub(r"\1", markdown)
    text = re.sub(r"[`*_]", "", text)
    return " ".join(text.lower().split()).rstrip(".:")


def _anchor(heading: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", _plain(heading))
    return "#" + re.sub(r"\s+", "-", slug)


def _names(item: str, heading: str) -> bool:
    plain_heading = _plain(heading)
    if plain_heading and plain_heading in _plain(item):
        return True
    return _anchor(heading) in {
        target.strip().lower() for _, target in _MARKDOWN_LINK.findall(item)
    }


def contents_problem(text: str) -> str | None:
    """What stops ``text`` from having a contents list, or ``None`` when it has one.

    A contents list starts in the first 100 lines and names every H2 heading below it, by
    the heading's text or by a link to its anchor. It may name more (a step list has
    entries that are not sections).
    """
    headings, lists = _outline(text)
    if not headings:
        return "has no H2 sections for a contents list to name"
    closest: list[str] | None = None
    for candidate in lists:
        if candidate.start >= _CONTENTS_WITHIN_LINES:
            break
        below = [heading for index, heading in headings if index > candidate.end]
        if not below:
            continue
        left_out = [
            heading
            for heading in below
            if not any(_names(item, heading) for item in candidate.items)
        ]
        if not left_out:
            return None
        if len(left_out) < len(below) and (
            closest is None or len(left_out) < len(closest)
        ):
            closest = left_out
    if closest is None:
        return f"has no list in its first {_CONTENTS_WITHIN_LINES} lines naming its H2 sections"
    return (
        f"has no list in its first {_CONTENTS_WITHIN_LINES} lines naming every H2 section below "
        "it; the closest leaves out: " + "; ".join(closest)
    )


def _contents_failures(
    tree: SkillTree, markdown: str, skill_md: str
) -> list[CheckFailure]:
    text = tree.text(markdown)
    if markdown == skill_md:
        is_long = len(_body_lines(text)) > _CONTENTS_SKILL_MD_BODY_LINES
    else:
        is_long = len(text.splitlines()) > _CONTENTS_REFERENCE_LINES
    problem = contents_problem(text) if is_long else None
    if problem is None:
        return []
    return [
        CheckFailure(
            Check.CONTENTS, f"{markdown} ({len(text.splitlines())} lines) {problem}"
        )
    ]


def check_failures(
    skill_dir: Path, tree: SkillTree | None = None
) -> list[CheckFailure]:
    """Every failure of the five checks for the skill at ``<root>/.agents/skills/<name>``.

    Exemptions are not applied here: this is what the skill fails, whatever it is excused.
    """
    skill_dir = skill_dir.resolve()
    if tree is None:
        tree = SkillTree(skill_dir.parent.parent.parent)
    skill = tree.relative(skill_dir)
    skill_md = f"{skill}/SKILL.md"
    crawl = _crawl(tree, skill_md, skill)

    failures = _size_failures(tree, skill_md)
    reached = set(crawl.depth) | _imported_projects(tree, skill, crawl.depth)
    for unreached in sorted(tree.own_nodes(skill) - reached):
        failures.append(
            CheckFailure(Check.REACHABILITY, f"{unreached} is linked from nothing")
        )
    markdown_files = _checked_markdown(crawl, skill, tree)
    for markdown in markdown_files:
        if crawl.depth[markdown] > _MAX_DEPTH:
            failures.append(
                CheckFailure(
                    Check.DEPTH,
                    f"{markdown} is {crawl.depth[markdown]} links away: {crawl.chain(markdown)}",
                )
            )
    for markdown in markdown_files:
        for missing in _missing_paths(tree, markdown):
            failures.append(
                CheckFailure(
                    Check.PATHS, f"{markdown} names {missing}, which does not exist"
                )
            )
    for markdown in markdown_files:
        failures.extend(_contents_failures(tree, markdown, skill_md))
    return failures


def skill_dirs(repo_root: Path) -> list[Path]:
    """Every directory under ``.agents/skills/`` that holds a SKILL.md."""
    return sorted(
        skill_md.parent for skill_md in (repo_root / _SKILLS_DIR).glob("*/SKILL.md")
    )


def unreached_shared_files(repo_root: Path, tree: SkillTree | None = None) -> list[str]:
    """The files under ``.agents/shared/`` that nothing reaches, relative to the root.

    A shared file is reached when a skill's crawl gets to it, when ``AGENTS.md``'s does, or
    when a program under ``system/`` names it by its path (a template a service fills in).
    """
    if tree is None:
        tree = SkillTree(repo_root)
    reached: set[str] = set()
    for skill_dir in skill_dirs(tree.root):
        skill = tree.relative(skill_dir)
        reached |= set(_crawl(tree, f"{skill}/SKILL.md", skill).depth)
    if tree.is_file("AGENTS.md"):
        reached |= set(_crawl(tree, "AGENTS.md", None).depth)
    for source in tree.files_under("system"):
        if source.endswith(".py") and not is_test_file(Path(source)):
            text = tree.text(source)
            if _SHARED_DIR in text:
                reached |= tree.links(source, None)
    return sorted(tree.own_nodes(_SHARED_DIR) - reached)
