"""Tests for the apps an update stops to free memory: that each one is on the run record before
the next is touched, that a program supervisorctl refused to stop is left off the record while one
whose stop never answered goes on it, and that the record outlives the chat that made it.

The supervisord side is a fake ``supervisorctl`` on PATH, so the tests exercise the real shell-out.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
import update_app_pause
import update_apply_contract
import update_self

_LEAD = "update-to-minds-v0-9-0"

_FAKE_SUPERVISORCTL = """#!/usr/bin/env python3
import json, os, sys

state = json.load(open(os.environ["FAKE_SUPERVISORCTL_STATE"]))
action, program = sys.argv[1], sys.argv[2]
with open(os.environ["FAKE_SUPERVISORCTL_CALLS"], "a") as calls:
    calls.write(f"{action} {program}\\n")
reason = state.get("refuse", {}).get(action, {}).get(program)
if reason is not None:
    print(f"{program}: ERROR ({reason})")
    sys.exit(1)
if program in state.get("already", {}).get(action, []):
    print(f"{program}: ERROR ({'not running' if action == 'stop' else 'already started'})")
    sys.exit(0)
running = set(state.get("running", []))
running.discard(program) if action == "stop" else running.add(program)
state["running"] = sorted(running)
json.dump(state, open(os.environ["FAKE_SUPERVISORCTL_STATE"], "w"))
print(f"{program}: {'stopped' if action == 'stop' else 'started'}")
"""


class FakeSupervisord:
    """A ``supervisorctl`` on PATH answering as supervisor 4.2.5's does: a refusal exits 1 with its
    reason on stdout, a redundant stop or start prints its ERROR line and exits 0."""

    def __init__(self, bin_dir: Path, state_path: Path, calls_path: Path) -> None:
        self.state_path = state_path
        self.calls_path = calls_path
        bin_dir.mkdir(parents=True, exist_ok=True)
        script = bin_dir / "supervisorctl"
        script.write_text(_FAKE_SUPERVISORCTL)
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
        self.set_state()

    def set_state(
        self,
        running: list[str] | None = None,
        refuse: dict[str, dict[str, str]] | None = None,
        already: dict[str, list[str]] | None = None,
    ) -> None:
        self.state_path.write_text(
            json.dumps(
                {
                    "running": running or [],
                    "refuse": refuse or {},
                    "already": already or {},
                }
            )
        )

    @property
    def running(self) -> list[str]:
        return list(json.loads(self.state_path.read_text())["running"])

    @property
    def calls(self) -> list[str]:
        if not self.calls_path.exists():
            return []
        return [line for line in self.calls_path.read_text().splitlines() if line]


@pytest.fixture
def supervisord(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeSupervisord:
    bin_dir = tmp_path / "fake-supervisord-bin"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_SUPERVISORCTL_STATE", str(tmp_path / "supervisord.json"))
    monkeypatch.setenv("FAKE_SUPERVISORCTL_CALLS", str(tmp_path / "supervisord-calls"))
    monkeypatch.setenv("MNGR_AGENT_NAME", _LEAD)
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-lead")
    monkeypatch.setenv("MINDS_CHAT_ID", "agent-lead")
    return FakeSupervisord(
        bin_dir, tmp_path / "supervisord.json", tmp_path / "supervisord-calls"
    )


def _main(tmp_path: Path, *args: str) -> int:
    return update_self.main([*args, "--repo-root", str(tmp_path)])


def _record(tmp_path: Path) -> update_apply_contract.RunStatus:
    status = update_apply_contract.read_run_status(tmp_path)
    assert status is not None
    return status


def test_each_app_is_on_the_record_before_the_next_one_is_touched(
    tmp_path: Path, supervisord: FakeSupervisord
) -> None:
    """A pass shed partway through its own stopping must still be able to put back what it took."""
    supervisord.set_state(running=["daily-digest", "pr-review", "reading-room"])
    assert _main(tmp_path, "run-status", "start") == 0

    assert _main(tmp_path, "run-status", "stop-apps", "daily-digest", "pr-review") == 0

    assert _record(tmp_path).stopped_programs == ["daily-digest", "pr-review"]
    assert supervisord.running == ["reading-room"]
    assert supervisord.calls == ["stop daily-digest", "stop pr-review"]


def test_an_app_that_would_not_stop_is_not_recorded_as_one_to_start(
    tmp_path: Path, supervisord: FakeSupervisord
) -> None:
    supervisord.set_state(
        running=["daily-digest", "ghost"], refuse={"stop": {"ghost": "no such process"}}
    )
    assert _main(tmp_path, "run-status", "start") == 0

    assert _main(tmp_path, "run-status", "stop-apps", "ghost", "daily-digest") == 1

    assert _record(tmp_path).stopped_programs == ["daily-digest"]


def test_a_stop_that_times_out_is_reported_but_still_recorded(
    tmp_path: Path,
    supervisord: FakeSupervisord,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A timeout says the answer never came, not that the app is up. Recording one that turns out
    to be running costs a redundant start the resume forgets; not recording one that is down
    leaves it stopped with nothing to bring it back."""
    assert _main(tmp_path, "run-status", "start") == 0
    slow = tmp_path / "slow-supervisorctl"
    slow.mkdir()
    script = slow / "supervisorctl"
    script.write_text("#!/bin/sh\nexec sleep 30\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    monkeypatch.setenv("PATH", f"{slow}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(update_app_pause, "STOP_TIMEOUT_SECONDS", 0.3)

    assert _main(tmp_path, "run-status", "stop-apps", "daily-digest") == 1

    assert _record(tmp_path).stopped_programs == ["daily-digest"]
    # The record is not a claim that the app is down, so the round must not read as one: the
    # lead composes the results message from this output.
    printed = capsys.readouterr().out
    assert "Stopped daily-digest." not in printed
    assert "never answered" in printed


def test_an_app_already_stopped_counts_as_stopped(
    tmp_path: Path, supervisord: FakeSupervisord
) -> None:
    """supervisorctl answers a redundant stop with ``ERROR (not running)``; the state the caller
    wanted is the one it has, so the app is still this pass's to start again."""
    supervisord.set_state(already={"stop": ["daily-digest"]})
    assert _main(tmp_path, "run-status", "start") == 0

    assert _main(tmp_path, "run-status", "stop-apps", "daily-digest") == 0

    assert _record(tmp_path).stopped_programs == ["daily-digest"]


def test_resume_starts_every_recorded_app_and_empties_the_record(
    tmp_path: Path, supervisord: FakeSupervisord
) -> None:
    supervisord.set_state(running=["daily-digest", "pr-review"])
    assert _main(tmp_path, "run-status", "start") == 0
    assert _main(tmp_path, "run-status", "stop-apps", "daily-digest", "pr-review") == 0

    assert _main(tmp_path, "run-status", "resume-apps") == 0

    assert _record(tmp_path).stopped_programs == []
    assert supervisord.running == ["daily-digest", "pr-review"]
    # Re-running is a no-op rather than a second round of starts.
    assert _main(tmp_path, "run-status", "resume-apps") == 0
    assert supervisord.calls.count("start daily-digest") == 1


def test_an_app_a_landed_apply_already_restarted_leaves_the_record(
    tmp_path: Path, supervisord: FakeSupervisord
) -> None:
    """The ordinary ending: the apply restarts supervisord, every program comes back under
    `autostart=true`, and the resume meets `ERROR (already started)` for each. That is the outcome
    the record asked for, so the record empties and the results message has no caveat to carry."""
    supervisord.set_state(running=["daily-digest", "pr-review"])
    assert _main(tmp_path, "run-status", "start") == 0
    assert _main(tmp_path, "run-status", "stop-apps", "daily-digest", "pr-review") == 0
    supervisord.set_state(
        running=["daily-digest", "pr-review"],
        already={"start": ["daily-digest", "pr-review"]},
    )

    assert _main(tmp_path, "run-status", "resume-apps") == 0

    assert _record(tmp_path).stopped_programs == []


def test_an_app_that_will_not_start_stays_on_the_record_for_the_next_try(
    tmp_path: Path, supervisord: FakeSupervisord
) -> None:
    supervisord.set_state(running=["daily-digest", "broken"])
    assert _main(tmp_path, "run-status", "start") == 0
    assert _main(tmp_path, "run-status", "stop-apps", "broken", "daily-digest") == 0
    supervisord.set_state(running=[], refuse={"start": {"broken": "spawn error"}})

    assert _main(tmp_path, "run-status", "resume-apps") == 1

    assert _record(tmp_path).stopped_programs == ["broken"]
    assert supervisord.running == ["daily-digest"]


def test_an_app_the_update_removed_is_reported_once_and_dropped_from_the_record(
    tmp_path: Path, supervisord: FakeSupervisord, capsys: pytest.CaptureFixture[str]
) -> None:
    """No later resume could start a program supervisord no longer has, so keeping it would carry
    it into every run after and report it as down in every results message."""
    supervisord.set_state(running=["daily-digest", "old-digest"])
    assert _main(tmp_path, "run-status", "start") == 0
    assert _main(tmp_path, "run-status", "stop-apps", "old-digest", "daily-digest") == 0
    supervisord.set_state(
        running=[], refuse={"start": {"old-digest": "no such process"}}
    )
    capsys.readouterr()

    assert _main(tmp_path, "run-status", "resume-apps") == 1

    assert _record(tmp_path).stopped_programs == []
    assert supervisord.running == ["daily-digest"]
    captured = capsys.readouterr()
    assert "Started old-digest." not in captured.out
    assert "old-digest is no longer a program in this workspace" in captured.err
    assert _main(tmp_path, "run-status", "resume-apps") == 0


def test_a_new_run_carries_over_apps_the_last_one_never_started(
    tmp_path: Path, supervisord: FakeSupervisord, capsys: pytest.CaptureFixture[str]
) -> None:
    """The record outlives the chat that wrote it: nothing else ever starts these."""
    supervisord.set_state(running=["daily-digest"])
    assert _main(tmp_path, "run-status", "start") == 0
    assert _main(tmp_path, "run-status", "stop-apps", "daily-digest") == 0

    assert (
        _main(tmp_path, "run-status", "start", "--chat", "update-to-minds-v0-9-1") == 0
    )

    assert _record(tmp_path).stopped_programs == ["daily-digest"]
    assert "Carried over 1 app(s)" in capsys.readouterr().out
    assert (
        _main(tmp_path, "run-status", "resume-apps", "--chat", "update-to-minds-v0-9-1")
        == 0
    )
    assert supervisord.running == ["daily-digest"]
