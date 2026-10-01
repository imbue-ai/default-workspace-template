"""Tests for the /proc process-tree walk, against a fake ``/proc`` layout."""

from pathlib import Path

from oom_priority.proctree import list_descendant_pids


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
