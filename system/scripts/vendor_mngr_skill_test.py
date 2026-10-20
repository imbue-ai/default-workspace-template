"""Tests for the vendored-skill writer and check: the committed copy of the Imbue Studio skill is
written from, and must equal, the skill at the pinned mngr commit, read through an injected reader
so no test touches GitHub."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import vendor_mngr_skill
from list_mngr_plugins import GitPin

_PUBLIC_SHA = "a" * 40
_INTERNAL_SHA = "b" * 40
_PUBLIC_REPO = "https://github.com/imbue-ai/mngr"
_INTERNAL_REPO = "https://github.com/imbue-ai/mngr-internal"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _mngr_checkout_with_skill(path: Path, text: str) -> tuple[Path, str]:
    """A git checkout holding the skill at its mngr path in one commit; returns it with that commit."""
    path.mkdir()
    _git(path, "init", "-q")
    skill = path / vendor_mngr_skill.SKILL_SOURCE_PATH
    skill.parent.mkdir(parents=True)
    skill.write_text(text)
    _git(path, "add", "-A")
    _git(
        path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "the skill"
    )
    return path, _git(path, "rev-parse", "HEAD")


def _write_pin(root: Path, repo_url: str, rev: str) -> None:
    (root / "pyproject.toml").write_text(
        "[tool.uv.sources]\n"
        f'imbue-mngr = {{ git = "{repo_url}", rev = "{rev}", subdirectory = "libs/mngr" }}\n'
    )


def _write_vendored(root: Path, text: str) -> None:
    vendored = root / vendor_mngr_skill.SKILL_VENDORED_PATH
    vendored.parent.mkdir(parents=True)
    vendored.write_text(text)


def test_writing_copies_the_skill_at_the_given_commit(tmp_path: Path) -> None:
    pin = GitPin(git_url=_PUBLIC_REPO, rev=_PUBLIC_SHA)

    is_changed = vendor_mngr_skill.write_vendored_skill(
        tmp_path, pin, lambda pin: f"skill at {pin.rev}\n"
    )

    assert is_changed
    assert (
        tmp_path / vendor_mngr_skill.SKILL_VENDORED_PATH
    ).read_text() == f"skill at {_PUBLIC_SHA}\n"


def test_writing_leaves_a_matching_copy_untouched(tmp_path: Path) -> None:
    _write_vendored(tmp_path, "the skill\n")
    vendored = tmp_path / vendor_mngr_skill.SKILL_VENDORED_PATH
    before = vendored.stat().st_mtime_ns
    pin = GitPin(git_url=_PUBLIC_REPO, rev=_PUBLIC_SHA)

    is_changed = vendor_mngr_skill.write_vendored_skill(
        tmp_path, pin, lambda pin: "the skill\n"
    )

    assert not is_changed
    assert vendored.stat().st_mtime_ns == before


def test_a_matching_copy_passes_the_check(tmp_path: Path) -> None:
    _write_pin(tmp_path, _PUBLIC_REPO, _PUBLIC_SHA)
    _write_vendored(tmp_path, "the skill\n")

    vendor_mngr_skill.check_vendored_skill(tmp_path, lambda pin: "the skill\n")


def test_a_differing_copy_fails_the_check_naming_the_pinned_commit(
    tmp_path: Path,
) -> None:
    _write_pin(tmp_path, _PUBLIC_REPO, _PUBLIC_SHA)
    _write_vendored(tmp_path, "an edited skill\n")

    with pytest.raises(vendor_mngr_skill.VendoredSkillError, match=_PUBLIC_SHA[:12]):
        vendor_mngr_skill.check_vendored_skill(tmp_path, lambda pin: "the skill\n")


def test_a_missing_copy_fails_the_check(tmp_path: Path) -> None:
    _write_pin(tmp_path, _PUBLIC_REPO, _PUBLIC_SHA)

    with pytest.raises(vendor_mngr_skill.VendoredSkillError, match="missing"):
        vendor_mngr_skill.check_vendored_skill(tmp_path, lambda pin: "the skill\n")


def test_the_check_reads_the_pin_from_pyproject(tmp_path: Path) -> None:
    _write_pin(tmp_path, _INTERNAL_REPO, _INTERNAL_SHA)
    _write_vendored(tmp_path, "the skill\n")
    seen: list[GitPin] = []

    def read(pin: GitPin) -> str:
        seen.append(pin)
        return "the skill\n"

    vendor_mngr_skill.check_vendored_skill(tmp_path, read)

    assert [(pin.git_url, pin.rev) for pin in seen] == [(_INTERNAL_REPO, _INTERNAL_SHA)]


def test_the_raw_url_points_at_the_skill_in_the_pinned_repo_and_commit() -> None:
    pin = GitPin(git_url=_PUBLIC_REPO, rev=_PUBLIC_SHA)

    assert vendor_mngr_skill.raw_file_url(pin) == (
        f"https://raw.githubusercontent.com/imbue-ai/mngr/{_PUBLIC_SHA}/apps/minds/skills_plugin/skills/imbue-studio/SKILL.md"
    )


def test_a_private_pin_without_a_token_asks_for_a_local_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(vendor_mngr_skill.INTERNAL_GIT_TOKEN_ENV_VAR, raising=False)
    pin = GitPin(git_url=_INTERNAL_REPO, rev=_INTERNAL_SHA)

    with pytest.raises(vendor_mngr_skill.VendoredSkillError, match="--mngr-repo"):
        vendor_mngr_skill.read_skill_from_github(pin)


def test_the_checkout_reader_reads_the_skill_at_the_commit(tmp_path: Path) -> None:
    checkout, sha = _mngr_checkout_with_skill(tmp_path / "mngr", "the skill\n")
    (checkout / vendor_mngr_skill.SKILL_SOURCE_PATH).write_text("edited since\n")

    read = vendor_mngr_skill.checkout_skill_reader(checkout)

    assert read(GitPin(git_url=_PUBLIC_REPO, rev=sha)) == "the skill\n"


def test_the_checkout_reader_names_the_commit_it_cannot_read(tmp_path: Path) -> None:
    checkout, _ = _mngr_checkout_with_skill(tmp_path / "mngr", "the skill\n")

    read = vendor_mngr_skill.checkout_skill_reader(checkout)

    with pytest.raises(vendor_mngr_skill.VendoredSkillError, match=_PUBLIC_SHA[:12]):
        read(GitPin(git_url=_PUBLIC_REPO, rev=_PUBLIC_SHA))


def test_writing_from_the_command_line_copies_the_skill_and_prints_the_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    checkout, sha = _mngr_checkout_with_skill(tmp_path / "mngr", "the skill\n")
    root = tmp_path / "template"
    root.mkdir()
    _write_pin(root, _PUBLIC_REPO, _PUBLIC_SHA)
    argv = [
        "--write",
        "--mngr-repo",
        str(checkout),
        "--rev",
        sha,
        "--repo-root",
        str(root),
    ]

    assert vendor_mngr_skill.main(argv) == 0

    assert (root / vendor_mngr_skill.SKILL_VENDORED_PATH).read_text() == "the skill\n"
    assert capsys.readouterr().out == f"{vendor_mngr_skill.SKILL_VENDORED_PATH}\n"
    assert vendor_mngr_skill.main(argv) == 0
    assert capsys.readouterr().out == ""


def test_the_check_refuses_a_commit_argument(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exit_info:
        vendor_mngr_skill.main(
            ["--check", "--rev", _PUBLIC_SHA, "--repo-root", str(tmp_path)]
        )

    assert exit_info.value.code == 2
