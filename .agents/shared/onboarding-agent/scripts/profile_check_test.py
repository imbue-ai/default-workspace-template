"""Tests for ``profile_check.py``.

Run via: ``uv run pytest .agents/shared/onboarding-agent/scripts/profile_check_test.py``
"""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent / "profile_check.py"
_spec = importlib.util.spec_from_file_location("profile_check", _SCRIPT)
assert _spec is not None and _spec.loader is not None
profile_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(profile_check)

_PROSE = (
    "You coordinate volunteer shifts for a food bank, and right now that lives in texts and spreadsheets, "
    "which seems to be where most of your time goes. You are at home in Excel and Google Sheets and have "
    "never needed to write code, so plain, point-and-click things suit you."
)

_VALID = f"""# About you
Written by your onboarding agent on 2026-10-24. It is a plain file: open, edit or delete it any time. Anyone you give access to your whole workspace could see it.

{_PROSE} You are the [Volunteer Coordinator at Oakland Community Food Bank](https://example.org/ocfb/staff).

Where this came from: what you told me, the apps you chose to share, a public page you confirmed.
"""


def test_a_valid_portrait_passes() -> None:
    assert profile_check.check_profile(_VALID) == []


def test_the_file_must_start_with_the_heading() -> None:
    problems = profile_check.check_profile(_VALID.replace("# About you", "# Notes"))
    assert any("must start with" in problem for problem in problems)


def test_the_file_must_end_with_its_sources() -> None:
    problems = profile_check.check_profile(
        _VALID.replace("Where this came from:", "Origin:")
    )
    assert any("Where this came from" in problem for problem in problems)

    after = _VALID + "\nOne more line after the sources.\n"
    assert any(
        "Where this came from" in problem
        for problem in profile_check.check_profile(after)
    )


def test_a_portrait_over_the_word_cap_is_flagged() -> None:
    long = _VALID + " word" * profile_check.MAX_WORDS
    problems = profile_check.check_profile(long)
    assert any("more than the" in problem for problem in problems)


@pytest.mark.parametrize(
    ("sentence", "expected"),
    [
        ("Reach you at maya@example.org.", "email address"),
        ("Call 555-123-4567.", "phone number"),
        ("You live at 42 Maple Street.", "street address"),
        ("Your password is hunter2.", "credential"),
        ("Key a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4.", "credential"),
    ],
)
def test_plainly_private_content_is_flagged(sentence: str, expected: str) -> None:
    problems = profile_check.check_profile(
        _VALID.replace(_PROSE, _PROSE + " " + sentence)
    )
    assert any(expected in problem for problem in problems)


def test_a_link_is_not_read_as_content() -> None:
    link = "[a talk](https://example.org/a/1234567890123/talks/abcdefghijklmnopqrstuvwxyz0123456789)"
    assert (
        profile_check.check_profile(_VALID.replace(_PROSE, _PROSE + " " + link)) == []
    )


def test_main_prints_ok_and_exits_zero_for_a_valid_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "about-you.md"
    path.write_text(_VALID, encoding="utf-8")
    assert profile_check.main([str(path)]) == 0
    assert capsys.readouterr().out.strip() == "ok"


def test_main_exits_one_and_lists_problems_for_an_invalid_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "about-you.md"
    path.write_text(
        _VALID.replace(_PROSE, _PROSE + " Reach you at maya@example.org."),
        encoding="utf-8",
    )
    assert profile_check.main([str(path)]) == 1
    assert "email address" in capsys.readouterr().err


def test_main_exits_one_for_a_missing_file(tmp_path: Path) -> None:
    assert profile_check.main([str(tmp_path / "missing.md")]) == 1


@pytest.mark.parametrize(
    "phrase",
    [
        "which suit you rather than building software yourself",
        "not just scripts",
        "tools instead of code",
        "you've never written any",
        "you haven't needed a developer",
        "you don't code",
        "this has never written code",
        "the thing that costs you time isn't any one project",
        "this is not a hobby but a job",
    ],
)
def test_saying_what_the_person_is_not_is_flagged(phrase: str) -> None:
    problems = profile_check.check_profile(
        _VALID.replace(_PROSE, _PROSE + " " + phrase + ".")
    )
    assert any("rewrite" in problem for problem in problems)


def test_the_valid_portrait_has_no_contrast_phrasing() -> None:
    assert not any(
        "rewrite" in problem for problem in profile_check.check_profile(_VALID)
    )


def test_main_reads_a_draft_from_standard_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(_VALID))
    assert profile_check.main(["-"]) == 0
    assert capsys.readouterr().out.strip() == "ok"


_WITH_IDEAS = _VALID.replace(
    "\nWhere this came from:",
    "\n## Ideas to build\n"
    "- A volunteer shift board (built in this setup), because shifts live in texts and spreadsheets.\n"
    "- A weekly reminder for unfilled shifts, because covering gaps is where your time goes.\n"
    "- A holiday-drive signup page, because you coordinate the holiday drive.\n"
    "\nWhere this came from:",
)


def test_a_portrait_with_an_ideas_list_passes() -> None:
    assert profile_check.check_profile(_WITH_IDEAS) == []


def test_more_ideas_than_the_cap_is_flagged() -> None:
    many = _WITH_IDEAS.replace(
        "\nWhere this came from:",
        "".join(f"- Idea {i}\n" for i in range(profile_check.MAX_IDEAS))
        + "\nWhere this came from:",
    )
    assert any(
        "ideas, more than" in problem for problem in profile_check.check_profile(many)
    )


def test_bullets_outside_the_ideas_list_are_not_counted_as_ideas() -> None:
    text = _VALID.replace(
        "\nWhere this came from:", "\n" + "- aside\n" * 10 + "\nWhere this came from:"
    )
    assert not any(
        "ideas, more than" in problem for problem in profile_check.check_profile(text)
    )
