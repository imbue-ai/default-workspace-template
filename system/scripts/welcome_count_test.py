"""Tests for welcome_count.py: the count the welcome skill varies its greeting by."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import welcome_count


def test_each_run_prints_the_number_of_earlier_runs_and_records_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(welcome_count.ENV_COUNT_PATH, str(tmp_path / "state" / "count"))

    printed = []
    for _ in range(3):
        assert welcome_count.main([]) == 0
        printed.append(capsys.readouterr().out.strip())

    assert printed == ["0", "1", "2"]
    assert (tmp_path / "state" / "count").read_text() == "3\n"


def test_a_peek_prints_the_count_without_recording_a_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "count"
    monkeypatch.setenv(welcome_count.ENV_COUNT_PATH, str(path))
    path.write_text("4\n")

    assert welcome_count.main(["--peek"]) == 0

    assert capsys.readouterr().out.strip() == "4"
    assert path.read_text() == "4\n"


def test_an_unreadable_count_reads_as_none_and_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "count"
    path.write_text("many")
    assert welcome_count.read_count(path) == 0
    assert "not a number" in capsys.readouterr().err
    # An absent file is the expected first run: nothing to warn about.
    assert welcome_count.read_count(tmp_path / "absent") == 0
    assert capsys.readouterr().err == ""


def test_the_default_path_is_under_the_workspaces_machine_state(tmp_path: Path) -> None:
    assert (
        welcome_count.count_path({}, tmp_path)
        == tmp_path / "data" / ".state" / "welcome" / "count"
    )
