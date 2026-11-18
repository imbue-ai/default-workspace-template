"""Tests for the what's-new listing ``changelog-entries`` prints."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from update_self_skill import update_self
from update_self_skill.update_runtime import Runner
from update_self_skill.update_whats_new import CHANGELOG_PATH, list_whats_new

_RELEASED = ("0.10.6", "0.10.5", "0.9.5")
_UNRELEASED = "## [Unreleased]\n\n- Not released yet."


def _section(version: str) -> str:
    return f"## [minds-v{version}] - 2026-11-01\n\n- Summary of {version}.\n"


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


def _changelog(*versions: str, unreleased: str = "## [Unreleased]\n") -> dict[str, str]:
    """The root ``CHANGELOG.md`` of a release holding ``versions``, newest first."""
    return {
        CHANGELOG_PATH: "\n".join(
            ["# Changelog\n", unreleased, *map(_section, versions)]
        )
    }


@pytest.fixture
def template(tmp_path: Path) -> Path:
    """Releases minds-v0.9.3 (no ``CHANGELOG.md``), minds-v0.9.5, minds-v0.10.5 and
    minds-v0.10.6; an untagged ``main`` with an ``[Unreleased]`` line; a ``workspace``
    branch on minds-v0.9.3; and a ``rewritten`` branch with no release."""
    repo = tmp_path / "template"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, {"README.md": "0.9.3\n"}, "minds-v0.9.3")
    _commit(repo, _changelog("0.9.5"), "minds-v0.9.5")
    _commit(repo, _changelog("0.10.5", "0.9.5"), "minds-v0.10.5")
    _commit(repo, _changelog(*_RELEASED), "minds-v0.10.6")
    _commit(repo, _changelog(*_RELEASED, unreleased=f"{_UNRELEASED}\n"))
    _git(repo, "checkout", "-q", "-b", "workspace", "minds-v0.9.3")
    _commit(repo, {"notes.md": "Mine.\n"})
    _git(repo, "checkout", "-q", "--orphan", "rewritten")
    _commit(repo, {"README.md": "Rewritten.\n"})
    return repo


@pytest.mark.parametrize(
    ("base", "target", "release", "sections", "note"),
    [
        pytest.param(
            "workspace",
            "minds-v0.10.6",
            "minds-v0.9.3",
            ["minds-v0.10.6", "minds-v0.10.5", "minds-v0.9.5"],
            "Releases before minds-v0.9.5 are not summarized.",
            id="older-than-every-section",
        ),
        pytest.param(
            "minds-v0.9.5",
            "minds-v0.10.6",
            "minds-v0.9.5",
            ["minds-v0.10.6", "minds-v0.10.5"],
            None,
            id="on-the-oldest-section",
        ),
        pytest.param(
            "minds-v0.10.6",
            "main",
            "minds-v0.10.6",
            ["Unreleased"],
            None,
            id="untagged-target",
        ),
    ],
)
def test_each_release_newer_than_the_workspaces_is_listed(
    template: Path,
    base: str,
    target: str,
    release: str,
    sections: list[str],
    note: str | None,
) -> None:
    answer = list_whats_new(base, target, template, Runner(), None)

    assert answer == {"release": release, "sections": sections, "note": note}


def test_a_workspace_with_no_release_found_gets_the_newest_release(
    template: Path,
) -> None:
    assert list_whats_new("rewritten", "minds-v0.10.6", template, Runner(), None) == {
        "release": None,
        "sections": ["minds-v0.10.6"],
        "note": "The workspace's release is unknown, so only the newest is listed.",
    }


def test_a_target_without_a_changelog_lists_nothing(template: Path) -> None:
    assert list_whats_new("workspace", "minds-v0.9.3", template, Runner(), None) == {
        "release": "minds-v0.9.3",
        "sections": [],
        "note": "minds-v0.9.3 has no CHANGELOG.md, so nothing is listed.",
    }


@pytest.mark.parametrize(
    ("base", "target", "sections", "text"),
    [
        (
            "minds-v0.10.5",
            "main",
            ["Unreleased", "minds-v0.10.6"],
            f"{_UNRELEASED}\n\n{_section('0.10.6')}",
        ),
        ("minds-v0.10.6", "minds-v0.10.6", [], ""),
    ],
)
def test_the_command_prints_the_listing_and_writes_the_sections(
    template: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    base: str,
    target: str,
    sections: list[str],
    text: str,
) -> None:
    out = tmp_path / "scratch" / "whats-new.md"
    argv = ["changelog-entries", "--base", base, "--target", target]

    code = update_self.main([*argv, "--out", str(out), "--repo-root", str(template)])

    assert code == 0
    assert json.loads(capsys.readouterr().out)["sections"] == sections
    assert out.read_text() == text
