from pathlib import Path

from oom_priority.badness import find_earlyoom_argv, snapshot_processes


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


def test_a_non_utf8_argv_is_still_read(tmp_path: Path) -> None:
    entry = tmp_path / "546"
    entry.mkdir()
    (entry / "comm").write_text("earlyoom\n")
    (entry / "cmdline").write_bytes(b"earlyoom\0--avoid\0^caf\xe9\0")
    found = find_earlyoom_argv(tmp_path)
    assert found is not None and found[1][:2] == ["earlyoom", "--avoid"]


def test_a_gvisor_process_mapping_a_non_utf8_path_keeps_its_anonymous_memory(
    tmp_path: Path,
) -> None:
    entry = tmp_path / "77"
    entry.mkdir()
    (entry / "comm").write_text("python3\n")
    (entry / "oom_score_adj").write_text("900\n")
    (entry / "status").write_text(
        "Name:\tpython3\nVmSize:\t90000 kB\nVmRSS:\t9000 kB\nThreads:\t1\n"
    )
    (entry / "smaps").write_bytes(
        b"7f00-7f10 r--p 00000000 00:2a 77 /data/caf\xe9.bin\nAnonymous:          120 kB\n"
    )
    assert [
        (sample.pid, sample.rss_kib) for sample in snapshot_processes(tmp_path)
    ] == [(77, 120)]
