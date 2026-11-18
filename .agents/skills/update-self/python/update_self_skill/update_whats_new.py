"""An update's "what's new": the target's root changelog sections for each release newer
than the workspace's."""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from update_self_skill.update_ledger import release_of
from update_self_skill.update_runtime import Runner
from update_self_skill.update_target import Version, parse_version

CHANGELOG_PATH = "CHANGELOG.md"
UNABRIDGED_CHANGELOG_PATH = "UNABRIDGED_CHANGELOG.md"
_HEADING_PREFIX = "## ["
_UNRELEASED = "Unreleased"


class Section(NamedTuple):
    label: str
    path: str
    text: str


def parse_sections(text: str) -> dict[str, str]:
    """Each ``## [<label>]`` section of ``text`` by label, heading included."""
    lines_by_label: dict[str, list[str]] = {}
    lines: list[str] = []
    for line in text.split("\n"):
        if line.startswith(_HEADING_PREFIX):
            label = line.removeprefix(_HEADING_PREFIX).split("]")[0]
            lines = lines_by_label.setdefault(label, [])
        lines.append(line)
    return {label: "\n".join(body).rstrip() for label, body in lines_by_label.items()}


def _show(runner: Runner, repo_root: Path, ref: str, path: str) -> str | None:
    shown = runner.run(
        ["git", "show", f"{ref}:{path}"],
        cwd=str(repo_root),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    return shown.stdout if shown.returncode == 0 else None


def _releases(path: str, text: str) -> dict[Version, Section]:
    return {
        version: Section(label, path, section)
        for label, section in parse_sections(text).items()
        if (version := parse_version(label)) is not None
    }


def list_whats_new(
    base: str, target: str, repo_root: Path, runner: Runner, out: Path | None
) -> dict[str, object]:
    """What ``changelog-entries`` prints; ``out`` gets the listed sections' text."""
    release = release_of(repo_root, runner, base)
    floor = parse_version(release)
    changelog = _show(runner, repo_root, target, CHANGELOG_PATH)
    sections: list[Section] = []
    note = None
    if changelog is None:
        note = f"{target} has no {CHANGELOG_PATH}, so nothing is listed."
    else:
        unabridged = _show(runner, repo_root, target, UNABRIDGED_CHANGELOG_PATH)
        by_version = _releases(UNABRIDGED_CHANGELOG_PATH, unabridged or "")
        by_version.update(_releases(CHANGELOG_PATH, changelog))
        releases = sorted(by_version.items(), reverse=True)
        if floor is None:
            note = "The workspace's release is unknown, so only the newest is listed."
            releases = releases[:1]
        sections = [s for v, s in releases if floor is None or v > floor]
        unreleased = parse_sections(changelog).get(_UNRELEASED, "")
        if unreleased.partition("\n")[2].strip():
            sections.insert(0, Section(_UNRELEASED, CHANGELOG_PATH, unreleased))
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        text = "\n\n".join(section.text for section in sections)
        out.write_text(f"{text}\n" if text else "", encoding="utf-8")
    return {
        "release": release if floor is not None else None,
        "sections": [{"label": s.label, "file": s.path} for s in sections],
        "note": note,
    }
