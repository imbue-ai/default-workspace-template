"""Tests for the /proc process-tree walk and identity readers, against a fake ``/proc`` layout."""

import os
import threading
from pathlib import Path

import pytest

from oom_priority.proctree import is_thread, list_descendant_pids, read_start_ticks
from oom_priority.testing import write_fake_process


def _write_fake_proc_children(proc_dir: Path, pid: int, children: list[int]) -> None:
    task_dir = proc_dir / str(pid) / "task" / str(pid)
    task_dir.mkdir(parents=True)
    (task_dir / "children").write_text(" ".join(str(child) for child in children) + " ")


def test_descendant_walk_is_recursive_and_survives_gaps(tmp_path: Path) -> None:
    # 10 -> 11 -> 12, plus 10 -> 13 where 13 has no /proc entry (exited).
    _write_fake_proc_children(tmp_path, 10, [11, 13])
    _write_fake_proc_children(tmp_path, 11, [12])
    _write_fake_proc_children(tmp_path, 12, [])
    found = list_descendant_pids(10, proc_dir=tmp_path)
    assert sorted(found) == [11, 12, 13]


def test_descendant_walk_on_a_host_without_proc(tmp_path: Path) -> None:
    assert list_descendant_pids(10, proc_dir=tmp_path / "none") == []


def _write_fake_proc_status(proc_dir: Path, pid: int, tgid: int) -> None:
    status = proc_dir / str(pid) / "status"
    status.parent.mkdir(parents=True, exist_ok=True)
    status.write_text(f"Name:\tchrome\nTgid:\t{tgid}\nPid:\t{pid}\nVmRSS:\t1000 kB\n")


def test_descendant_walk_leaves_out_threads_listed_as_children(tmp_path: Path) -> None:
    # gVisor lists a child process's threads beside it in ``children``: 14 is a thread of 11.
    _write_fake_proc_children(tmp_path, 10, [11, 14])
    _write_fake_proc_children(tmp_path, 11, [12])
    _write_fake_proc_children(tmp_path, 12, [])
    _write_fake_proc_status(tmp_path, 11, tgid=11)
    _write_fake_proc_status(tmp_path, 12, tgid=12)
    _write_fake_proc_status(tmp_path, 14, tgid=11)
    found = list_descendant_pids(10, proc_dir=tmp_path)
    assert sorted(found) == [11, 12]


def test_start_ticks_are_read_past_a_command_name_with_spaces_and_parentheses(tmp_path: Path) -> None:
    write_fake_process(tmp_path, 42, start_ticks=123456, comm="a) b (c")
    assert read_start_ticks(42, proc_dir=tmp_path) == 123456
    assert read_start_ticks(43, proc_dir=tmp_path) is None


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs a real /proc")
def test_a_real_process_has_a_start_time_and_its_thread_is_not_a_process() -> None:
    assert read_start_ticks(os.getpid()) is not None
    assert not is_thread(os.getpid())

    is_running = threading.Event()
    is_done = threading.Event()
    thread = threading.Thread(target=lambda: (is_running.set(), is_done.wait(5)))
    thread.start()
    try:
        assert is_running.wait(5)
        assert thread.native_id is not None
        assert is_thread(thread.native_id)
    finally:
        is_done.set()
        thread.join()
