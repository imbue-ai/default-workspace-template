import json
import os
from pathlib import Path

import pytest

from oom_priority import registry
from oom_priority.testing import write_fake_process

_HAS_PROC = Path("/proc/self/stat").exists()


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OOM_PRIORITY_RUNTIME_DIR", str(tmp_path / "runtime"))
    return tmp_path / "runtime"


@pytest.fixture
def proc(tmp_path: Path) -> Path:
    return tmp_path / "proc"


def test_record_and_lookup_round_trip(runtime: Path, proc: Path) -> None:
    registry.record_agent_pid(4242, "alpha", is_worker=True, proc_dir=proc)
    found = registry.lookup_agent(4242)
    assert found is not None
    assert found["agent_name"] == "alpha"
    assert found["is_worker"] is True


def test_lookup_unknown_pid_returns_none(runtime: Path) -> None:
    assert registry.lookup_agent(4242) is None


def test_every_live_process_of_an_agent_is_found(runtime: Path, proc: Path) -> None:
    # codex registers its TUI and its app-server daemon under one id.
    for pid in (4243, 4242):
        write_fake_process(proc, pid, start_ticks=1000 + pid)
        registry.record_agent_pid(pid, "alpha", is_worker=False, agent_id="agent-abc", proc_dir=proc)
    write_fake_process(proc, 5000, start_ticks=7)
    registry.record_agent_pid(5000, "beta", is_worker=False, agent_id="agent-def", proc_dir=proc)

    assert registry.lookup_pids_by_agent_id("agent-abc", proc_dir=proc) == [4242, 4243]
    assert registry.lookup_pids_by_agent_id("agent-other", proc_dir=proc) == []


def test_a_pid_reused_by_a_later_process_is_not_the_agents(runtime: Path, proc: Path) -> None:
    write_fake_process(proc, 4242, start_ticks=100)
    registry.record_agent_pid(4242, "alpha", is_worker=False, agent_id="agent-abc", proc_dir=proc)
    assert registry.lookup_pids_by_agent_id("agent-abc", proc_dir=proc) == [4242]

    # The agent exits and an unrelated process starts with the same pid.
    write_fake_process(proc, 4242, start_ticks=900, comm="sleep")

    assert registry.lookup_pids_by_agent_id("agent-abc", proc_dir=proc) == []


def test_an_id_that_is_now_another_processs_thread_is_not_the_agents(runtime: Path, proc: Path) -> None:
    # The agent's pid is now a thread id of the chat app: /proc/<id> resolves, and a write to
    # its oom_score_adj would re-band the whole chat app.
    directory = registry.agent_pids_dir()
    directory.mkdir(parents=True)
    (directory / "4242.json").write_text(json.dumps({"agent_name": "alpha", "agent_id": "agent-abc"}))
    write_fake_process(proc, 849, start_ticks=5, comm="chat-app")
    write_fake_process(proc, 4242, start_ticks=900, tgid=849, comm="chat-app")

    assert registry.lookup_pids_by_agent_id("agent-abc", proc_dir=proc) == []


def test_an_entry_without_a_start_time_is_never_counted_but_kept_while_its_pid_lives(
    runtime: Path, proc: Path
) -> None:
    # Written before start times were recorded: its pid alone cannot tell its agent from
    # whatever reused the pid, so no reader counts it, but the kill hook can still name it.
    directory = registry.agent_pids_dir()
    directory.mkdir(parents=True)
    (directory / "4242.json").write_text(json.dumps({"agent_name": "alpha", "agent_id": "agent-abc"}))
    (directory / "5000.json").write_text(json.dumps({"agent_name": "beta", "agent_id": "agent-def"}))
    write_fake_process(proc, 4242, start_ticks=100)

    assert registry.lookup_pids_by_agent_id("agent-abc", proc_dir=proc) == []
    registry.prune_stale_entries(proc_dir=proc)
    assert sorted(path.name for path in directory.iterdir()) == ["4242.json"]
    assert registry.lookup_agent(4242) is not None


def test_an_entry_from_an_earlier_boot_is_not_the_agents(runtime: Path, proc: Path) -> None:
    # The registry outlives a container restart, and a start time only counts from boot, so a
    # new process can hold an old entry's pid and start time.
    boot_id = proc / "sys" / "kernel" / "random" / "boot_id"
    boot_id.parent.mkdir(parents=True)
    boot_id.write_text("boot-one\n")
    write_fake_process(proc, 4242, start_ticks=100)
    registry.record_agent_pid(4242, "alpha", is_worker=False, agent_id="agent-abc", proc_dir=proc)
    assert registry.lookup_pids_by_agent_id("agent-abc", proc_dir=proc) == [4242]

    boot_id.write_text("boot-two\n")

    assert registry.lookup_pids_by_agent_id("agent-abc", proc_dir=proc) == []
    registry.prune_stale_entries(proc_dir=proc)
    assert registry.lookup_agent(4242) is None


def test_lookup_ignores_entries_without_an_id(runtime: Path, proc: Path) -> None:
    # An entry recorded before agent_id was captured must not match any id lookup.
    write_fake_process(proc, 4242, start_ticks=100)
    registry.record_agent_pid(4242, "alpha", is_worker=False, proc_dir=proc)
    assert registry.lookup_pids_by_agent_id("agent-abc", proc_dir=proc) == []


def test_prune_keeps_only_entries_that_still_name_their_process(runtime: Path, proc: Path) -> None:
    for pid, start_ticks in ((100, 1), (200, 2), (300, 3)):
        write_fake_process(proc, pid, start_ticks=start_ticks)
        registry.record_agent_pid(pid, f"agent-{pid}", is_worker=False, agent_id=f"agent-{pid}", proc_dir=proc)
    directory = registry.agent_pids_dir()
    (directory / "400.json").write_text("{not json")
    (directory / "500.json").write_text("{not json")
    write_fake_process(proc, 400, start_ticks=4)

    write_fake_process(proc, 200, start_ticks=99)  # reused
    write_fake_process(proc, 300, start_ticks=3, tgid=100)  # now a thread of 100
    registry.prune_stale_entries(proc_dir=proc)

    # 100 is still its process; 400 is unreadable (maybe mid-write) but its pid is live; 500 is gone.
    assert sorted(path.name for path in directory.iterdir()) == ["100.json", "400.json"]


def test_recording_prunes_stale_entries_but_keeps_the_new_one(runtime: Path, proc: Path) -> None:
    directory = registry.agent_pids_dir()
    directory.mkdir(parents=True)
    (directory / "999.json").write_text(json.dumps({"agent_name": "stale"}))

    registry.record_agent_pid(123, "beta", is_worker=False, proc_dir=proc)

    assert not (directory / "999.json").exists()
    assert registry.lookup_agent(123) is not None


@pytest.mark.skipif(not _HAS_PROC, reason="needs a real /proc")
def test_this_process_is_found_through_the_real_proc(runtime: Path) -> None:
    registry.record_agent_pid(os.getpid(), "self", is_worker=False, agent_id="agent-self")
    assert registry.lookup_pids_by_agent_id("agent-self") == [os.getpid()]
