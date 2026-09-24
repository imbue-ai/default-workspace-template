"""Tests for the OOM drill's prediction and its wrong-victim check.

The drill itself needs real memory pressure and a running earlyoom, so only its
pure pieces are tested here; the drill is run for real in the staging rehearsal
and the AWS release test.
"""

import json
import re
from collections import deque
from pathlib import Path

import oom_drill
from oom_drill import ProcessSample

# One oom_score_adj point is worth (MemTotal + SwapTotal) / 1000 KiB: 1000 KiB here.
_TOTAL_KIB = 1_000_000
_AVOID = re.compile(r"^(sshd|supervisord|earlyoom|tini)$|^tmux")


def _sample(
    pid: int, comm: str, adj: int, rss: int | None, swap: int = 0, pte: int = 0
) -> ProcessSample:
    return ProcessSample(
        pid=pid,
        comm=comm,
        oom_score_adj=adj,
        vm_rss_kib=rss,
        vm_swap_kib=swap,
        vm_pte_kib=pte,
    )


_WORKSPACE = [
    _sample(1, "tini", 0, 500),
    _sample(7, "supervisord", 0, 40_000),
    _sample(20, "earlyoom", 0, 1_000),
    _sample(30, "python3", 20, 400_000),  # system_interface
    _sample(31, "chat-app", 25, 300_000),
    _sample(40, "claude", 560, 250_000),  # an idle chat
    _sample(50, "pytest", 900, 20_000),
    _sample(60, "tilion", 1000, 150_000),  # a Chromium renderer
    _sample(70, "python3", -1000, 600_000),  # the drill's hog
    _sample(80, "kworker", 0, None),  # no mm
]


def test_prediction_follows_the_bands_and_skips_what_earlyoom_cannot_pick() -> None:
    ranking = oom_drill.predict_ranking(
        _WORKSPACE, _TOTAL_KIB, _AVOID, excluded_pids={20}
    )

    # system_interface (30) outranks the chat app (31) despite its lower band:
    # 100000 KiB more RSS outweighs 5 band points, the soft steer.
    assert [process.pid for process in ranking] == [60, 50, 40, 30, 31, 7]
    # pid 7 is --avoid-ed: 40000 KiB of RSS, 300 points below 0.
    assert ranking[-1].badness_kib == 40_000 - 300_000
    assert ranking[0].badness_kib == 150_000 + 1000 * 1000


def test_swap_and_page_tables_count_like_rss() -> None:
    ranking = oom_drill.predict_ranking(
        [_sample(100, "a", 100, 1000), _sample(101, "b", 100, 1000, swap=500, pte=10)],
        _TOTAL_KIB,
        None,
        excluded_pids=set(),
    )
    assert [(process.pid, process.badness_kib) for process in ranking] == [
        (101, 101_510),
        (100, 101_000),
    ]


def test_the_top_predicted_victim_is_right_and_anything_below_it_is_wrong() -> None:
    ranking = oom_drill.predict_ranking(
        _WORKSPACE, _TOTAL_KIB, _AVOID, excluded_pids={20}
    )

    right = oom_drill.judge_kill(60, ranking, tolerance_kib=1024)
    wrong = oom_drill.judge_kill(40, ranking, tolerance_kib=1024)

    assert (right.verdict, right.is_protected_shed_before_expendable) == (
        "right",
        False,
    )
    assert wrong.verdict == "wrong"
    assert wrong.top is not None and wrong.top.pid == 60


def test_a_near_tie_within_the_tolerance_is_not_a_wrong_victim() -> None:
    ranking = oom_drill.predict_ranking(
        [_sample(100, "a", 25, 30_000), _sample(101, "b", 24, 30_500)],
        _TOTAL_KIB,
        None,
        excluded_pids=set(),
    )
    # 55000 vs 54500: pid 101 is 500 KiB short of the top, inside a 1024 KiB tolerance.
    assert oom_drill.judge_kill(101, ranking, tolerance_kib=1024).verdict == "right"
    assert oom_drill.judge_kill(101, ranking, tolerance_kib=100).verdict == "wrong"


def test_a_service_shed_while_something_at_900_remains_is_flagged() -> None:
    # The service is the top pick (a huge RSS outweighs the band gap), so the
    # prediction calls it right, but a pytest at 900 is still there.
    samples = [_sample(30, "python3", 20, 950_000), _sample(50, "pytest", 900, 20_000)]
    ranking = oom_drill.predict_ranking(samples, _TOTAL_KIB, None, excluded_pids=set())

    judgement = oom_drill.judge_kill(30, ranking, tolerance_kib=1024)

    assert judgement.verdict == "right"
    assert judgement.is_protected_shed_before_expendable


def test_a_victim_that_started_after_the_snapshot_is_unpredicted() -> None:
    ranking = oom_drill.predict_ranking(
        _WORKSPACE, _TOTAL_KIB, _AVOID, excluded_pids=set()
    )
    assert (
        oom_drill.judge_kill(999, ranking, tolerance_kib=1024).verdict == "unpredicted"
    )


def test_a_kill_is_judged_against_the_last_snapshot_that_still_has_its_victim() -> None:
    before = oom_drill.Snapshot(
        1.0, [_sample(60, "tilion", 1000, 1), _sample(50, "pytest", 900, 1)]
    )
    # Taken after earlyoom killed pid 60 but before the drill reaped it: a
    # zombie, with no VmRSS.
    zombie = oom_drill.Snapshot(
        1.5, [_sample(60, "tilion", 1000, None), _sample(50, "pytest", 900, 1)]
    )
    after = oom_drill.Snapshot(2.0, [_sample(50, "pytest", 900, 1)])
    snapshots = deque([before, zombie, after])

    assert oom_drill.last_snapshot_with(60, snapshots) is before
    assert oom_drill.last_snapshot_with(50, snapshots) is after
    assert oom_drill.last_snapshot_with(99, snapshots) is None


def test_status_memory_reads_gvisor_and_kernel_threads() -> None:
    assert oom_drill.parse_status_memory(
        "Name:\tx\nVmRSS:\t  812 kB\nVmPTE:\t 64 kB\nVmSwap:\t 3 kB\n"
    ) == (812, 3, 64)
    # gVisor serves no VmSwap or VmPTE.
    assert oom_drill.parse_status_memory("Name:\tx\nVmRSS:\t812 kB\n") == (812, 0, 0)
    assert oom_drill.parse_status_memory("Name:\tkthreadd\nThreads:\t1\n") == (
        None,
        0,
        0,
    )


def test_avoid_regex_is_read_from_earlyoom_argv() -> None:
    argv = ["/usr/local/bin/earlyoom", "-m", "10,5", "--avoid", "^(sshd|tini)$|^tmux"]
    avoid = oom_drill.parse_avoid_regex(argv)
    assert (
        avoid is not None
        and avoid.search("tmux: server")
        and not avoid.search("python3")
    )
    assert oom_drill.parse_avoid_regex(["earlyoom", "-r", "0"]) is None


def test_ledger_reads_only_whole_new_lines(tmp_path: Path) -> None:
    ledger = tmp_path / "shed.jsonl"
    old = json.dumps({"type": "process_shed", "pid": 1})
    ledger.write_text(old + "\n")
    offset = ledger.stat().st_size
    shed = json.dumps({"type": "process_shed", "pid": 2})
    notice = json.dumps({"type": "notice_delivered", "agent_name": "a"})
    with open(ledger, "a") as handle:
        handle.write(shed + "\n" + notice + "\n" + '{"type": "process_sh')

    records, offset = oom_drill.read_new_ledger_kills(ledger, offset)
    assert [record["pid"] for record in records] == [2]

    # The partial line is picked up once it is complete.
    with open(ledger, "a") as handle:
        handle.write('ed", "pid": 3}\n')
    records, _offset = oom_drill.read_new_ledger_kills(ledger, offset)
    assert [record["pid"] for record in records] == [3]
