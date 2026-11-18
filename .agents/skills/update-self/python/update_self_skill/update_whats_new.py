"""An update's "what's new": the target's root CHANGELOG.md section for each release
newer than the workspace's."""

from __future__ import annotations

from pathlib import Path

from update_self_skill.update_ledger import release_of
from update_self_skill.update_runtime import Runner
from update_self_skill.update_target import parse_version

CHANGELOG_PATH = "CHANGELOG.md"
_HEADING_PREFIX = "## ["
_UNRELEASED = "Unreleased"


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


def list_whats_new(
    base: str, target: str, repo_root: Path, runner: Runner, out: Path | None
) -> dict[str, object]:
    """What ``changelog-entries`` prints; ``out`` gets the listed sections' text."""
    release = release_of(repo_root, runner, base)
    floor = parse_version(release)
    shown = runner.run(
        ["git", "show", f"{target}:{CHANGELOG_PATH}"],
        cwd=str(repo_root),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    by_label = parse_sections(shown.stdout) if shown.returncode == 0 else {}
    releases = sorted(
        (
            (version, label)
            for label in by_label
            if (version := parse_version(label)) is not None
        ),
        reverse=True,
    )
    labels = [label for version, label in releases if floor is None or version > floor]
    note = None
    if shown.returncode != 0:
        note = f"{target} has no {CHANGELOG_PATH}, so nothing is listed."
    elif floor is None:
        note = "The workspace's release is unknown, so only the newest is listed."
        labels = labels[:1]
    elif releases and floor < releases[-1][0]:
        note = f"Releases before {releases[-1][1]} are not summarized."
    if by_label.get(_UNRELEASED, "").partition("\n")[2].strip():
        labels.insert(0, _UNRELEASED)
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        text = "\n\n".join(by_label[label] for label in labels)
        out.write_text(f"{text}\n" if text else "", encoding="utf-8")
    return {
        "release": release if floor is not None else None,
        "sections": labels,
        "note": note,
    }
