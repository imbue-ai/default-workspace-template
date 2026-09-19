"""Tests for the secret-file wrapper: the env file is parsed exactly as the chat app writes
it, the child sees the variables, and a misplaced or over-readable file is refused."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import _load_script_module

with_secrets = _load_script_module("with_secrets_for_tests", "with_secrets.py")

_SCRIPT = Path(__file__).parent / "with_secrets.py"

# One value with every shell-significant character: a quote, a space, a dollar, a
# backslash, a hash, an equals sign, a newline, and a trailing space.
_AWKWARD_VALUE = "it's $HOME \\ # a=b\nsecond line "


def _secrets_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "data" / ".secrets"
    directory.mkdir(parents=True)
    return directory


def _write_env(directory: Path, text: str, mode: int = 0o600) -> Path:
    env_file = directory / "svc.env"
    env_file.write_text(text, encoding="utf-8")
    env_file.chmod(mode)
    return env_file


def _quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _child_environment(env_file: Path, *names: str) -> dict[str, str | None]:
    """Run the wrapper for real, with a child that reports the named variables as JSON."""
    reporter = "import json, os, sys; print(json.dumps({n: os.environ.get(n) for n in sys.argv[1:]}))"
    completed = subprocess.run(
        [sys.executable, str(_SCRIPT), str(env_file), "--", sys.executable, "-c", reporter, *names],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_a_value_with_every_shell_significant_character_round_trips_into_the_child(tmp_path: Path) -> None:
    env_file = _write_env(_secrets_dir(tmp_path), f"API_KEY={_quote(_AWKWARD_VALUE)}\nOTHER='plain'\n")
    assert _child_environment(env_file, "API_KEY", "OTHER") == {"API_KEY": _AWKWARD_VALUE, "OTHER": "plain"}


def test_the_child_keeps_the_parent_environment_and_the_file_wins_on_a_clash(tmp_path: Path) -> None:
    env_file = _write_env(_secrets_dir(tmp_path), "WITH_SECRETS_CLASH='from-file'\n")
    environment = {**os.environ, "WITH_SECRETS_CLASH": "from-parent", "WITH_SECRETS_KEEP": "kept"}
    reporter = "import json, os; print(json.dumps([os.environ['WITH_SECRETS_CLASH'], os.environ['WITH_SECRETS_KEEP']]))"
    completed = subprocess.run(
        [sys.executable, str(_SCRIPT), str(env_file), "--", sys.executable, "-c", reporter],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == ["from-file", "kept"]


def test_the_command_exit_status_is_the_wrappers(tmp_path: Path) -> None:
    env_file = _write_env(_secrets_dir(tmp_path), "X='1'\n")
    completed = subprocess.run(
        [sys.executable, str(_SCRIPT), str(env_file), "--", sys.executable, "-c", "raise SystemExit(37)"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 37


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("NAME='a'\\''b'", ("NAME", "a'b")),
        ('NAME="say \\"hi\\""', ("NAME", 'say "hi"')),
        ("NAME=bare value", ("NAME", "bare value")),
        ("export NAME='exported'", ("NAME", "exported")),
        ("  # a comment", None),
        ("", None),
    ],
)
def test_hand_written_and_generated_lines_parse(line: str, expected: tuple[str, str] | None) -> None:
    expected_variables = {} if expected is None else {expected[0]: expected[1]}
    assert with_secrets.parse_env_file(line + "\n") == expected_variables


def test_a_single_quoted_value_may_span_lines_and_the_next_line_still_parses() -> None:
    assert with_secrets.parse_env_file("A='one\ntwo'\nB='3'\n") == {"A": "one\ntwo", "B": "3"}


@pytest.mark.parametrize(
    "line", ["not an assignment", "1BAD='x'", "NAME='unterminated", "A-B='x'", "NAME='a' trailing"]
)
def test_a_malformed_line_is_refused_rather_than_skipped(line: str) -> None:
    with pytest.raises(with_secrets.WithSecretsError, match="line 1"):
        with_secrets.parse_env_file(line + "\n")


def test_a_later_line_wins_over_an_earlier_one() -> None:
    assert with_secrets.parse_env_file("A='1'\nA='2'\n") == {"A": "2"}


def test_a_group_readable_file_is_refused(tmp_path: Path) -> None:
    env_file = _write_env(_secrets_dir(tmp_path), "X='1'\n", mode=0o640)
    with pytest.raises(with_secrets.WithSecretsError, match="readable by more than its owner"):
        with_secrets.load_env_file(env_file)


def test_a_file_outside_the_secrets_directory_is_refused(tmp_path: Path) -> None:
    env_file = tmp_path / "svc.env"
    env_file.write_text("X='1'\n")
    env_file.chmod(0o600)
    with pytest.raises(with_secrets.WithSecretsError, match="not a data/.secrets"):
        with_secrets.load_env_file(env_file)


def test_a_missing_file_is_refused_with_its_path(tmp_path: Path) -> None:
    directory = _secrets_dir(tmp_path)
    with pytest.raises(with_secrets.WithSecretsError, match="does not exist"):
        with_secrets.load_env_file(directory / "absent.env")


def test_a_refusal_exits_two_and_runs_nothing(tmp_path: Path) -> None:
    env_file = _write_env(_secrets_dir(tmp_path), "X='1'\n", mode=0o644)
    marker = tmp_path / "ran"
    completed = subprocess.run(
        [sys.executable, str(_SCRIPT), str(env_file), "--", sys.executable, "-c", f"open({str(marker)!r}, 'w')"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 2
    assert "readable by more than its owner" in completed.stderr
    assert not marker.exists()


def test_a_missing_separator_is_a_usage_error() -> None:
    with pytest.raises(with_secrets.WithSecretsError, match="usage"):
        with_secrets.split_arguments(["data/.secrets/x.env", "echo", "hi"])
