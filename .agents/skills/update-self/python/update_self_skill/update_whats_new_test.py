"""Tests for the what's-new listing ``changelog-entries`` prints."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from update_self_skill import update_self
from update_self_skill.update_runtime import Runner
from update_self_skill.update_whats_new import (
    CHANGELOG_PATH,
    UNABRIDGED_CHANGELOG_PATH,
    list_whats_new,
)


def _section(version: str, body: str) -> str:
    return f"## [minds-v{version}] - 2026-11-01\n\n{body}\n"


_SUMMARY = {v: _section(v, f"- Summary of {v}.") for v in ("0.9.5", "0.10.5", "0.10.6")}
_FULL = {v: _section(v, f"- Entry of {v}.") for v in ("0.9.5", "0.10.5", "0.10.6")}
_FULL["0.9.4"] = _section(
    "0.9.4", "- Entry of 0.9.4.\n\n## An entry's heading\n\nMore."
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
        cwd=repo,
        capture_output=True,
        check=True,
    )


def _commit(repo: Path, files: dict[str, str], tag: str | None = None) -> None:
    for relative, text in files.items():
        (repo / relative).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", tag or "change")
    if tag is not None:
        _git(repo, "tag", tag)


def _root_changelogs(*versions: str, unreleased: str = "") -> dict[str, str]:
    """The root changelogs of a release holding ``versions``, newest first."""
    summary = [_SUMMARY[v] for v in versions if v in _SUMMARY]
    return {
        CHANGELOG_PATH: "# Changelog\n\n"
        + "\n".join([f"## [Unreleased]\n{unreleased}", *summary]),
        UNABRIDGED_CHANGELOG_PATH: "# Unabridged changelog\n\n"
        + "\n".join(_FULL[v] for v in versions),
    }


@pytest.fixture
def template(tmp_path: Path) -> Path:
    """Releases from minds-v0.9.3 (no root changelogs) to minds-v0.10.6, the summary
    starting at minds-v0.9.5; an untagged ``main`` with an ``[Unreleased]`` line; a
    ``workspace`` branch on minds-v0.9.3; and a ``rewritten`` branch with no release."""
    repo = tmp_path / "template"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, {"README.md": "0.9.3\n"}, "minds-v0.9.3")
    _commit(repo, {"README.md": "0.9.4\n"}, "minds-v0.9.4")
    _commit(repo, _root_changelogs("0.9.5", "0.9.4"), "minds-v0.9.5")
    _commit(repo, _root_changelogs("0.10.5", "0.9.5", "0.9.4"), "minds-v0.10.5")
    released = ("0.10.6", "0.10.5", "0.9.5", "0.9.4")
    _commit(repo, _root_changelogs(*released), "minds-v0.10.6")
    _commit(repo, _root_changelogs(*released, unreleased="\n- Not released yet.\n"))
    _git(repo, "checkout", "-q", "-b", "workspace", "minds-v0.9.3")
    _commit(repo, {"notes.md": "Mine.\n"})
    _git(repo, "checkout", "-q", "--orphan", "rewritten")
    _commit(repo, {"README.md": "Rewritten.\n"})
    return repo


def _listed(*labels_and_files: tuple[str, str]) -> list[dict[str, str]]:
    return [{"label": label, "file": file} for label, file in labels_and_files]


@pytest.mark.parametrize(
    ("base", "target", "release", "listed"),
    [
        pytest.param(
            "workspace",
            "minds-v0.10.6",
            "minds-v0.9.3",
            _listed(
                ("minds-v0.10.6", CHANGELOG_PATH),
                ("minds-v0.10.5", CHANGELOG_PATH),
                ("minds-v0.9.5", CHANGELOG_PATH),
                ("minds-v0.9.4", UNABRIDGED_CHANGELOG_PATH),
            ),
            id="older-than-every-summarized-release",
        ),
        pytest.param(
            "minds-v0.10.5",
            "minds-v0.10.6",
            "minds-v0.10.5",
            _listed(("minds-v0.10.6", CHANGELOG_PATH)),
            id="on-a-summarized-release",
        ),
        pytest.param(
            "minds-v0.10.6", "minds-v0.10.6", "minds-v0.10.6", [], id="on-the-newest"
        ),
        pytest.param(
            "minds-v0.10.6",
            "main",
            "minds-v0.10.6",
            _listed(("Unreleased", CHANGELOG_PATH)),
            id="untagged-target",
        ),
    ],
)
def test_each_release_newer_than_the_workspaces_is_listed(
    template: Path, base: str, target: str, release: str, listed: list[dict[str, str]]
) -> None:
    answer = list_whats_new(base, target, template, Runner(), None)

    assert answer == {"release": release, "sections": listed, "note": None}


def test_a_workspace_with_no_release_found_gets_the_newest_release(
    template: Path,
) -> None:
    assert list_whats_new("rewritten", "minds-v0.10.6", template, Runner(), None) == {
        "release": None,
        "sections": _listed(("minds-v0.10.6", CHANGELOG_PATH)),
        "note": "The workspace's release is unknown, so only the newest is listed.",
    }


def test_a_target_without_a_changelog_lists_nothing(template: Path) -> None:
    assert list_whats_new("workspace", "minds-v0.9.4", template, Runner(), None) == {
        "release": "minds-v0.9.3",
        "sections": [],
        "note": "minds-v0.9.4 has no CHANGELOG.md, so nothing is listed.",
    }


@pytest.mark.parametrize(
    ("base", "listed", "text"),
    [
        (
            "workspace",
            _listed(
                ("minds-v0.9.5", CHANGELOG_PATH),
                ("minds-v0.9.4", UNABRIDGED_CHANGELOG_PATH),
            ),
            f"{_SUMMARY['0.9.5'].rstrip()}\n\n{_FULL['0.9.4'].rstrip()}\n",
        ),
        ("minds-v0.9.5", [], ""),
    ],
)
def test_the_command_prints_the_listing_and_writes_the_sections(
    template: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    base: str,
    listed: list[dict[str, str]],
    text: str,
) -> None:
    out = tmp_path / "scratch" / "whats-new.md"
    argv = ["changelog-entries", "--base", base, "--target", "minds-v0.9.5"]

    code = update_self.main([*argv, "--out", str(out), "--repo-root", str(template)])

    assert code == 0
    assert json.loads(capsys.readouterr().out)["sections"] == listed
    assert out.read_text() == text
