from pathlib import Path

from oom_priority.badness import find_earlyoom_argv


def _process(proc: Path, pid: int, comm: str, argv: list[str]) -> None:
    entry = proc / str(pid)
    entry.mkdir(parents=True)
    (entry / "comm").write_text(f"{comm}\n")
    (entry / "cmdline").write_bytes(b"\0".join(part.encode() for part in argv) + b"\0")


def test_earlyoom_is_found_by_name_with_its_argv(tmp_path: Path) -> None:
    _process(tmp_path, 10, "bash", ["bash"])
    _process(
        tmp_path,
        546,
        "earlyoom",
        ["/usr/local/bin/earlyoom", "-m", "10,5", "--avoid", "^tmux"],
    )
    (tmp_path / "self").mkdir()
    assert find_earlyoom_argv(tmp_path) == (
        546,
        ["/usr/local/bin/earlyoom", "-m", "10,5", "--avoid", "^tmux"],
    )


def test_no_running_earlyoom_reads_as_none(tmp_path: Path) -> None:
    _process(tmp_path, 10, "bash", ["bash"])
    assert find_earlyoom_argv(tmp_path) is None
