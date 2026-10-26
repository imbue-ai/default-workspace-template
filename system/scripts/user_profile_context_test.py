"""The profile SessionStart hook prints the user's approved notes into a chat agent's context.

It must stay silent for every agent that is not a chat, and never fail: the first message of
every session waits for it.
"""

import json
import os
import subprocess
from pathlib import Path

_HOOK = Path(__file__).resolve().with_name("user_profile_context.sh")

_PROFILE = (
    "# About you\n"
    "Written by your onboarding agent on 2026-10-24.\n\n"
    "You coordinate volunteer shifts for a food bank.\n\n"
    "Where this came from: what you told me.\n"
)


def _run(
    profile: Path, role: str | None, *args: str
) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key != "MNGR_AGENT_ROLE"}
    env["USER_PROFILE_FILE"] = str(profile)
    if role is not None:
        env["MNGR_AGENT_ROLE"] = role
    return subprocess.run(
        ["bash", str(_HOOK), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
    )


def test_codex_gets_the_same_text_as_session_start_additional_context(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "about-you.md"
    profile.write_text(_PROFILE)

    result = _run(profile, "chat", "--codex")

    assert result.returncode == 0
    output = json.loads(result.stdout)["hookSpecificOutput"]
    assert output["hookEventName"] == "SessionStart"
    assert output["additionalContext"].rstrip("\n") == _run(
        profile, "chat"
    ).stdout.rstrip("\n")


def test_codex_agents_that_are_not_chats_get_nothing(tmp_path: Path) -> None:
    profile = tmp_path / "about-you.md"
    profile.write_text(_PROFILE)

    result = _run(profile, "worker", "--codex")

    assert (result.returncode, result.stdout) == (0, "")


def test_a_chat_agent_gets_the_profile_with_a_header_that_says_it_is_notes(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "about-you.md"
    profile.write_text(_PROFILE)

    result = _run(profile, "chat")

    assert result.returncode == 0
    assert "notes, not instructions" in result.stdout
    assert "You coordinate volunteer shifts for a food bank." in result.stdout
    assert result.stdout.index("notes, not instructions") < result.stdout.index(
        "You coordinate"
    )


def test_agents_that_are_not_chats_get_nothing(tmp_path: Path) -> None:
    profile = tmp_path / "about-you.md"
    profile.write_text(_PROFILE)

    for role in ("worker", "automation", None):
        result = _run(profile, role)
        assert result.returncode == 0
        assert result.stdout == ""


def test_a_missing_or_empty_profile_is_silent_and_succeeds(tmp_path: Path) -> None:
    missing = _run(tmp_path / "nope.md", "chat")
    assert (missing.returncode, missing.stdout) == (0, "")

    empty = tmp_path / "empty.md"
    empty.write_text("  \n\n")
    blank = _run(empty, "chat")
    assert (blank.returncode, blank.stdout) == (0, "")


def test_a_very_long_profile_is_cut_to_a_bounded_size(tmp_path: Path) -> None:
    profile = tmp_path / "about-you.md"
    profile.write_text("# About you\n" + "word " * 20000)

    result = _run(profile, "chat")

    assert result.returncode == 0
    assert len(result.stdout) < 9000
