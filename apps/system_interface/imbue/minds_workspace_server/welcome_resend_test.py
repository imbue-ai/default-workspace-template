"""Tests for the welcome_resend helper."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

from imbue.minds_workspace_server import welcome_resend


def test_strip_frontmatter_removes_yaml_block() -> None:
    text = "---\nname: x\n---\n\n# Body\nrest"
    assert welcome_resend._strip_frontmatter(text).startswith("\n# Body")


def test_strip_frontmatter_no_frontmatter_returns_input_unchanged() -> None:
    text = "# Body\nrest"
    assert welcome_resend._strip_frontmatter(text) == text


def test_extract_first_message_header_finds_inside_separator_block() -> None:
    body = "# Skill title\nblurb\n\n---\n\n### Welcome to Minds\n\nbody\n\n---\n"
    assert welcome_resend._extract_first_message_header(body) == "### Welcome to Minds"


def test_extract_first_message_header_returns_none_when_no_separator_block() -> None:
    assert welcome_resend._extract_first_message_header("# Just a header\nbody") is None


def test_read_welcome_opening_line_against_real_skill_file(tmp_path: Path) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_text(
        "---\nname: welcome\n---\n\n# Welcome the user\n\n"
        "Output the following:\n\n---\n\n### Welcome to Minds\n\nA Mind ...\n\n---\n"
    )
    assert welcome_resend.read_welcome_opening_line(skill) == "### Welcome to Minds"


def test_read_welcome_opening_line_falls_back_to_any_header(tmp_path: Path) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_text("---\nname: x\n---\n\n# Some other header\n\nbody\n")
    assert welcome_resend.read_welcome_opening_line(skill) == "# Some other header"


def test_pane_contains_welcome_true_when_present() -> None:
    pane = "blah\n### Welcome to Minds\nmore\n"
    assert welcome_resend._pane_contains_welcome(pane, "### Welcome to Minds") is True


def test_pane_contains_welcome_false_when_empty() -> None:
    assert welcome_resend._pane_contains_welcome("", "### Welcome to Minds") is False
    assert welcome_resend._pane_contains_welcome(None, "### Welcome to Minds") is False


def test_pane_contains_welcome_false_when_missing() -> None:
    pane = "something else entirely"
    assert welcome_resend._pane_contains_welcome(pane, "### Welcome to Minds") is False


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def test_check_and_resend_welcome_resends_when_pane_missing(tmp_path: Path) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_text("---\nname: w\n---\n\nIntro\n\n---\n\n### Welcome to Minds\n\nA Mind\n\n---\n")
    fake_capture = MagicMock(return_value="empty pane")
    fake_send = MagicMock(return_value=True)
    with (
        patch.object(welcome_resend, "_capture_agent_pane", fake_capture),
        patch.object(welcome_resend, "send_message", fake_send),
    ):
        resent = _run(welcome_resend.check_and_resend_welcome("my-agent", skill_path=skill))
    assert resent is True
    fake_send.assert_called_once_with("my-agent", "/welcome")


def test_check_and_resend_welcome_skips_when_pane_has_welcome(tmp_path: Path) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_text("---\nname: w\n---\n\nIntro\n\n---\n\n### Welcome to Minds\n\nA Mind\n\n---\n")
    fake_capture = MagicMock(return_value="### Welcome to Minds appears here")
    fake_send = MagicMock(return_value=True)
    with (
        patch.object(welcome_resend, "_capture_agent_pane", fake_capture),
        patch.object(welcome_resend, "send_message", fake_send),
    ):
        resent = _run(welcome_resend.check_and_resend_welcome("my-agent", skill_path=skill))
    assert resent is False
    fake_send.assert_not_called()


def test_check_and_resend_welcome_returns_false_when_skill_unreadable(tmp_path: Path) -> None:
    missing = tmp_path / "missing.md"
    resent = _run(welcome_resend.check_and_resend_welcome("a", skill_path=missing))
    assert resent is False
