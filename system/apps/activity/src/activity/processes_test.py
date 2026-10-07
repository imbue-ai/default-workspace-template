from pathlib import Path

from activity.processes import as_badness_sample
from activity.processes import parse_parent_pid
from activity.processes import read_process
from activity.processes import read_process_table
from activity.testing import write_fake_process


def test_the_parent_pid_is_read_and_a_missing_one_is_the_namespace_root() -> None:
    assert parse_parent_pid("Name:\tclaude\nPPid:\t13065\n") == 13065
    assert parse_parent_pid("Name:\tinit\n") == 0


def test_a_linux_process_counts_its_vm_rss_swap_command_line_and_priority(tmp_path: Path) -> None:
    write_fake_process(tmp_path, 4121, "claude", 4000, 410, 300, ["claude", "--resume", "7f3a"], swap_kib=20)
    process = read_process(4121, tmp_path)
    assert process is not None
    assert (process.parent_pid, process.rss_kib, process.swap_kib, process.oom_score_adj) == (4000, 410, 20, 300)
    assert process.command_line == "claude --resume 7f3a"


def test_a_gvisor_process_counts_its_anonymous_memory_not_its_inflated_vm_rss(tmp_path: Path) -> None:
    write_fake_process(
        tmp_path, 6101, "chromium", 1, 9000, 1000, ["chromium"], is_gvisor=True, anonymous_kib=[300, 52]
    )
    process = read_process(6101, tmp_path)
    assert process is not None and process.rss_kib == 352


def test_a_gvisor_process_mapping_a_file_whose_name_is_not_utf8_is_still_counted(tmp_path: Path) -> None:
    write_fake_process(tmp_path, 6102, "python3", 1, 9000, 0, ["python3"], is_gvisor=True)
    (tmp_path / "6102" / "smaps").write_bytes(
        b"7f00-7f10 r--p 00000000 00:2a 77 /data/caf\xe9.bin\nAnonymous:          120 kB\n"
    )
    process = read_process(6102, tmp_path)
    assert process is not None and process.rss_kib == 120


def test_a_process_without_memory_of_its_own_is_left_out_as_earlyoom_leaves_it_out(tmp_path: Path) -> None:
    write_fake_process(tmp_path, 7, "defunct", 1, 50, 1000, [])
    (tmp_path / "7" / "status").write_text("Name:\tdefunct\nPPid:\t1\nThreads:\t1\nVmSize:\t0 kB\nVmRSS:\t50 kB\n")
    assert read_process(7, tmp_path) is None

    write_fake_process(tmp_path, 8, "leader", 1, 50, 0, [])
    (tmp_path / "8" / "status").write_text("Name:\tleader\nPPid:\t1\nThreads:\t4\nVmSize:\t0 kB\nVmRSS:\t50 kB\n")
    leader = read_process(8, tmp_path)
    assert leader is not None and leader.rss_kib == 0

    write_fake_process(tmp_path, 2, "kthreadd", 0, None, 0, [])
    assert read_process(2, tmp_path) is None
    assert read_process(99999, tmp_path) is None


def test_the_table_holds_every_readable_process_in_pid_order(tmp_path: Path) -> None:
    write_fake_process(tmp_path, 300, "b", 1, 20, 0, ["b"])
    write_fake_process(tmp_path, 20, "a", 1, 10, 0, ["a"])
    write_fake_process(tmp_path, 2, "kthreadd", 0, None, 0, [])
    (tmp_path / "self").mkdir()
    assert [process.pid for process in read_process_table(tmp_path)] == [20, 300]
    assert read_process_table(tmp_path / "missing") == []


def test_a_reading_becomes_the_scoring_models_sample(tmp_path: Path) -> None:
    write_fake_process(tmp_path, 10, "pytest", 1, 900, 900, ["pytest"], swap_kib=30)
    process = read_process(10, tmp_path)
    assert process is not None
    sample = as_badness_sample(process)
    assert (sample.pid, sample.comm, sample.rss_kib, sample.vm_swap_kib, sample.oom_score_adj) == (
        10,
        "pytest",
        900,
        30,
        900,
    )
