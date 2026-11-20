from pathlib import Path

from app_store.process_memory import SupervisedProcessMemoryReader
from app_store.process_memory import descendants_of
from app_store.process_memory import parse_parent_pid
from app_store.process_memory import parse_resident_kib
from app_store.process_memory import tree_memory_mb


def _process(proc: Path, pid: int, parent: int, rss_kib: int | None, command: str = "app") -> None:
    folder = proc / str(pid)
    folder.mkdir(parents=True)
    (folder / "stat").write_text(f"{pid} ({command}) S {parent} 1 1 0 -1\n")
    (folder / "status").write_text("Name:\tapp\n" + ("" if rss_kib is None else f"VmRSS:\t{rss_kib} kB\n"))


def test_a_programs_memory_is_its_whole_process_trees(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    _process(proc, 100, 1, 51_200)
    _process(proc, 101, 100, 102_400, command="python kernel (notebook)")
    _process(proc, 102, 101, 10_240)
    _process(proc, 103, 100, None)
    _process(proc, 200, 1, 999_999)
    (proc / "self").mkdir()
    assert tree_memory_mb(100, proc) == 160


def test_the_proc_fields_are_read_however_the_command_is_named() -> None:
    assert parse_parent_pid("7 (a (weird) name) S 3 7 7 0") == 3
    assert parse_resident_kib("Name:\tx\nVmRSS:\t2048 kB\n") == 2048
    assert parse_resident_kib("Name:\tx\n") == 0
    assert descendants_of(1, {2: 1, 3: 2, 4: 9}) == {1, 2, 3}


def test_a_program_supervisord_does_not_run_uses_nothing() -> None:
    assert SupervisedProcessMemoryReader().program_memory_mb("no-such-program-anywhere") is None
