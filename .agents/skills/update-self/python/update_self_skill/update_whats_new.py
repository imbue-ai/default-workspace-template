"""What an update's "what's new" is made from: the release sections of the root
``CHANGELOG.md`` the workspace does not have yet, and the changelog entry files the
update adds.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import NamedTuple, Sequence

CHANGELOG_PATH = "CHANGELOG.md"
UNABRIDGED_CHANGELOG_PATH = "UNABRIDGED_CHANGELOG.md"

# The last release whose tree holds every released change as an entry file, e.g. ``minds-v1.2.3``.
# CLEANUP: remove this constant, the entries read at it and the sections it hides once no
# supported workspace predates the first release whose CHANGELOG.md has release sections.
LAST_TAG_WITH_ENTRY_FILES: str | None = "minds-v0.10.5"

_SECTION_HEADING_PREFIX = "## "
_LABELLED_HEADING_PREFIX = "## ["
_UNRELEASED_LABEL = "Unreleased"

# Entry files sit one level under a ``changelog/`` directory at any depth, outside the vendored
# snapshots; ``top`` anchors both pathspecs at the repository root whatever directory git runs in.
_ENTRY_PATHSPECS = (":(top,glob)**/changelog/*", ":(top,exclude)system/vendor")


class ChangelogSection(NamedTuple):
    """One ``## [<label>]`` section of a changelog: its label and its text, heading included."""

    label: str
    text: str


class EntriesAtRef(NamedTuple):
    """Entry files an update adds, and the ref their text is read at."""

    ref: str
    paths: list[str]


class WhatsNew(NamedTuple):
    """What an update brings a workspace, and what of it could not be read."""

    sections: list[ChangelogSection]
    added: EntriesAtRef
    added_before_consolidation: EntriesAtRef | None
    unlisted: list[str]


def parse_changelog_sections(text: str) -> list[ChangelogSection]:
    """The ``## [<label>]`` sections of ``text`` in order, each ending at the next ``## `` line."""
    lines = text.split("\n")
    heading_indexes = [
        index
        for index, line in enumerate(lines)
        if line.startswith(_SECTION_HEADING_PREFIX)
    ]
    sections: list[ChangelogSection] = []
    for start, end in zip(heading_indexes, [*heading_indexes[1:], len(lines)]):
        if lines[start].startswith(_LABELLED_HEADING_PREFIX):
            label = lines[start].removeprefix(_LABELLED_HEADING_PREFIX).split("]")[0]
            sections.append(
                ChangelogSection(label, "\n".join(lines[start:end]).rstrip())
            )
    return sections


def changelog_problems(text: str) -> list[str]:
    """The ``## `` headings of ``text`` that are not ``## [<label>]`` ones, and the labels it repeats."""
    stray = [
        line
        for line in text.split("\n")
        if line.startswith(_SECTION_HEADING_PREFIX)
        and not line.startswith(_LABELLED_HEADING_PREFIX)
    ]
    labels = [section.label for section in parse_changelog_sections(text)]
    repeated = [label for label in dict.fromkeys(labels) if labels.count(label) > 1]
    problems: list[str] = []
    if stray:
        problems.append(
            "has headings that are not '## [<label>]' ones: "
            + ", ".join(map(repr, stray))
        )
    if repeated:
        problems.append(
            "has more than one section for "
            + ", ".join(f"[{label}]" for label in repeated)
        )
    return problems


def release_labels(sections: Sequence[ChangelogSection]) -> list[str]:
    return [section.label for section in sections if section.label != _UNRELEASED_LABEL]


def _has_content(section: ChangelogSection) -> bool:
    return any(line.strip() for line in section.text.split("\n")[1:])


def select_unseen_sections(
    target_sections: Sequence[ChangelogSection],
    base_labels: Sequence[str],
    last_tag_with_entry_files: str | None,
) -> list[ChangelogSection]:
    """The target's sections a workspace on the base has not seen.

    A base with release labels has seen exactly those. A base with none predates the
    release sections, so it has seen ``last_tag_with_entry_files`` and every section below
    it. ``[Unreleased]`` counts only when it holds something.
    """
    seen = set(base_labels)
    if not seen and last_tag_with_entry_files is not None:
        target_labels = [section.label for section in target_sections]
        seen = {last_tag_with_entry_files}
        if last_tag_with_entry_files in target_labels:
            seen.update(target_labels[target_labels.index(last_tag_with_entry_files) :])
    return [
        section
        for section in target_sections
        if section.label not in seen
        and (section.label != _UNRELEASED_LABEL or _has_content(section))
    ]


def _git(args: Sequence[str], repo_root: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=repo_root, capture_output=True)


def _decoded(output: bytes) -> str:
    return output.decode("utf-8", errors="replace")


def _failure(result: subprocess.CompletedProcess[bytes]) -> str:
    return _decoded(result.stderr).strip() or f"git exited {result.returncode}"


def _read_changelog(ref: str, repo_root: Path) -> tuple[str, str | None]:
    """``CHANGELOG.md`` at ``ref`` (empty when the tree has none), and why it could not be read."""
    listed = _git(
        ["ls-tree", "--full-tree", "--name-only", ref, "--", CHANGELOG_PATH], repo_root
    )
    if listed.returncode != 0:
        return "", _failure(listed)
    if not listed.stdout.strip():
        return "", None
    shown = _git(["show", f"{ref}:{CHANGELOG_PATH}"], repo_root)
    if shown.returncode != 0:
        return "", _failure(shown)
    return _decoded(shown.stdout), None


def _added_entries(
    base: str, ref: str, repo_root: Path
) -> subprocess.CompletedProcess[bytes]:
    return _git(
        [
            "diff",
            "--name-only",
            "--diff-filter=A",
            "--no-renames",
            base,
            ref,
            "--",
            *_ENTRY_PATHSPECS,
        ],
        repo_root,
    )


def _names(result: subprocess.CompletedProcess[bytes]) -> list[str]:
    return [line for line in _decoded(result.stdout).splitlines() if line]


def _gather_whats_new(
    base: str,
    target: str,
    repo_root: Path,
    last_tag_with_entry_files: str | None,
) -> WhatsNew:
    """What updating a workspace from ``base`` to ``target`` brings.

    Raises ``subprocess.CalledProcessError`` when the entries between the two refs cannot
    be listed. Anything else that cannot be read is left out and named in ``unlisted``.
    """
    added = _added_entries(base, target, repo_root)
    added.check_returncode()
    unlisted: list[str] = []

    base_text, base_problem = _read_changelog(base, repo_root)
    if base_problem is None and (base_text_problems := changelog_problems(base_text)):
        base_problem = "; ".join(f"it {problem}" for problem in base_text_problems)
    target_text, target_problem = _read_changelog(target, repo_root)
    base_labels = release_labels(parse_changelog_sections(base_text))
    sections = select_unseen_sections(
        parse_changelog_sections(target_text), base_labels, last_tag_with_entry_files
    )
    if base_problem is not None:
        unlisted.append(
            f"{CHANGELOG_PATH} at {base} could not be read, so release sections the "
            f"workspace already has may be listed again and no entry is read at an "
            f"earlier release: {base_problem}"
        )
    if target_problem is not None:
        unlisted.append(
            f"{CHANGELOG_PATH} at {target} could not be read, so no release section "
            f"is listed: {target_problem}"
        )
    unlisted.extend(
        f"{CHANGELOG_PATH} at {target} {problem}, so the sections listed may be incomplete"
        for problem in changelog_problems(target_text)
    )

    # A base whose changelog has no release section predates the consolidation of the
    # entry files, so the releases it skipped are still entry files at the last such tag.
    added_before_consolidation: EntriesAtRef | None = None
    is_base_before_consolidation = base_problem is None and not base_labels
    if is_base_before_consolidation and last_tag_with_entry_files is not None:
        added_earlier = _added_entries(base, last_tag_with_entry_files, repo_root)
        if added_earlier.returncode == 0:
            added_before_consolidation = EntriesAtRef(
                last_tag_with_entry_files, _names(added_earlier)
            )
        else:
            unlisted.append(
                f"the changelog entries at {last_tag_with_entry_files} could not be "
                f"listed: {_failure(added_earlier)}"
            )
    return WhatsNew(
        sections=sections,
        added=EntriesAtRef(target, _names(added)),
        added_before_consolidation=added_before_consolidation,
        unlisted=unlisted,
    )


def _read_entries(entries: EntriesAtRef, repo_root: Path) -> dict[str, str]:
    """The text of each entry that can be read at the entries' ref, by path."""
    request = "".join(f"{entries.ref}:{path}\n" for path in entries.paths)
    batch = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=repo_root,
        input=request.encode("utf-8"),
        capture_output=True,
    )
    # A batch that was cut short answers for fewer objects than were asked for.
    if batch.returncode != 0:
        return {}
    # One answer per requested object, in order: ``<sha> <type> <size>``, the content and a
    # newline, or a single line such as ``<object> missing``.
    text_by_path: dict[str, str] = {}
    output = batch.stdout
    position = 0
    for path in entries.paths:
        header_end = output.find(b"\n", position)
        if header_end == -1:
            break
        header = output[position:header_end].split()
        position = header_end + 1
        if len(header) != 3 or not header[2].isdigit():
            continue
        size = int(header[2])
        if header[1] == b"blob":
            text_by_path[path] = _decoded(output[position : position + size])
        position += size + 1
    return text_by_path


def _render_whats_new(whats_new: WhatsNew, repo_root: Path) -> tuple[str, list[str]]:
    """The unseen sections, then each added entry's text, as one document; and the entries
    whose text could not be read."""
    blocks: list[str] = []
    unread: list[str] = []
    if whats_new.sections:
        blocks.append(f"# Release sections of {CHANGELOG_PATH} new to this workspace")
        blocks.extend(section.text for section in whats_new.sections)
    for entries in (whats_new.added, whats_new.added_before_consolidation):
        if entries is None or not entries.paths:
            continue
        blocks.append(
            f"# Changelog entries the update adds, as they are at {entries.ref}"
        )
        text_by_path = _read_entries(entries, repo_root)
        for path in entries.paths:
            if path in text_by_path:
                blocks.append(f"## {path}\n\n{text_by_path[path].strip()}")
            else:
                unread.append(f"{path} at {entries.ref} could not be read")
    return "\n\n".join(blocks) + "\n" if blocks else "", unread


def list_whats_new(
    base: str,
    target: str,
    repo_root: Path,
    # Where to write the listed sections and then each listed entry's text, if anywhere.
    out: Path | None,
    last_tag_with_entry_files: str | None,
) -> dict[str, object]:
    """What ``changelog-entries`` prints for an update from ``base`` to ``target``."""
    whats_new = _gather_whats_new(base, target, repo_root, last_tag_with_entry_files)
    unlisted = list(whats_new.unlisted)
    if out is not None:
        document, unread = _render_whats_new(whats_new, repo_root)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(document, encoding="utf-8")
        unlisted.extend(unread)
    earlier = whats_new.added_before_consolidation
    return {
        "sections": [section.label for section in whats_new.sections],
        "added": whats_new.added.paths,
        "added_before_consolidation": None
        if earlier is None
        else {"ref": earlier.ref, "paths": earlier.paths},
        "unlisted": unlisted,
    }
