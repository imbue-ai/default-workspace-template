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


def test_a_kill_is_judged_against_the_last_snapshot_before_its_victim_shrank() -> None:
    before = oom_drill.Snapshot(
        1.0,
        [_sample(60, "tilion", 1000, 200_000), _sample(50, "pytest", 900, 20_000)],
    )
    # Taken after earlyoom signalled pid 60: process_mrelease has freed its
    # memory while the process still exists.
    released = oom_drill.Snapshot(
        1.2, [_sample(60, "tilion", 1000, 0), _sample(50, "pytest", 900, 20_000)]
    )
    # Then an unreaped zombie, with no VmRSS.
    zombie = oom_drill.Snapshot(
        1.5,
        [_sample(60, "tilion", 1000, None), _sample(50, "pytest", 900, 20_500)],
    )
    after = oom_drill.Snapshot(2.0, [_sample(50, "pytest", 900, 20_000)])
    snapshots = deque([before, released, zombie, after])

    assert oom_drill.last_snapshot_with(60, snapshots, 1024, None) is before
    # pid 50 never shrank beyond the tolerance, so its newest snapshot counts.
    assert oom_drill.last_snapshot_with(50, snapshots, 1024, None) is after
    assert oom_drill.last_snapshot_with(99, snapshots, 1024, None) is None


def test_a_kill_is_judged_against_the_snapshots_from_before_earlyoom_chose_it() -> None:
    # earlyoom chose pid 60 (an interactive bash, which ignores SIGTERM) at
    # t=2.0, then waited seconds for it to exit. A process that started during
    # that wait outranks it, but was not there to be chosen.
    at_choice = oom_drill.Snapshot(1.5, [_sample(60, "bash", 200, 12_000)])
    during_wait = oom_drill.Snapshot(
        4.0,
        [_sample(60, "bash", 200, 12_000), _sample(61, "containerd", 200, 40_000)],
    )
    snapshots = deque([at_choice, during_wait])

    snapshot = oom_drill.last_snapshot_with(
        60, snapshots, tolerance_kib=1024, chosen_at=2.0
    )
    assert snapshot is at_choice
    ranking = oom_drill.predict_ranking(
        snapshot.samples, _TOTAL_KIB, _AVOID, excluded_pids=set()
    )
    assert oom_drill.judge_kill(60, ranking, tolerance_kib=1024).verdict == "right"


def test_a_victim_first_listed_after_earlyoom_chose_it_is_unpredicted() -> None:
    # pid 62, a service supervisord had just restarted, was chosen at t=2.0
    # before any snapshot listed it. Only a snapshot from the SIGTERM wait
    # does, next to a newcomer that outranks it.
    before_choice = oom_drill.Snapshot(1.5, [_sample(60, "python3", 300, 12_000)])
    during_wait = oom_drill.Snapshot(
        4.0,
        [
            _sample(60, "python3", 300, 12_000),
            _sample(62, "browser-service", 300, 30_000),
            _sample(63, "containerd", 300, 40_000),
        ],
    )
    snapshots = deque([before_choice, during_wait])

    assert oom_drill.last_snapshot_with(62, snapshots, 1024, chosen_at=2.0) is None
    judged = oom_drill.newest_snapshot_before(snapshots, chosen_at=2.0)
    assert judged is before_choice
    ranking = oom_drill.predict_ranking(
        judged.samples, _TOTAL_KIB, _AVOID, excluded_pids=set()
    )
    assert oom_drill.judge_kill(62, ranking, tolerance_kib=1024).verdict == (
        "unpredicted"
    )
    # Without a choice time, or with none of the snapshots that old, the
    # newest is all there is.
    assert oom_drill.newest_snapshot_before(snapshots, None) is during_wait
    assert oom_drill.newest_snapshot_before(snapshots, chosen_at=1.0) is during_wait


def test_earlyoom_kill_lines_are_read_whole_and_new(tmp_path: Path) -> None:
    log = tmp_path / "earlyoom-stderr.log"
    log.write_text('sending SIGTERM to process 5 uid 0 "old": oom_score 1\n')
    offset = log.stat().st_size
    with open(log, "a") as handle:
        handle.write(
            "mem avail:  3 of 3922 MiB ( 0.08%)\n"
            'sending SIGTERM to process 60 uid 0 "bash": oom_score 802\n'
            "escalating to SIGKILL after 5.103 seconds\n"
            "sending SIGKILL to process 61 uid"
        )

    lines, offset = oom_drill.read_new_kill_lines(log, offset)
    assert [pid for pid, _line in lines] == [60]

    with open(log, "a") as handle:
        handle.write(' 501 "containerd": oom_score 805\n')
    lines, _offset = oom_drill.read_new_kill_lines(log, offset)
    assert lines == [
        (61, 'sending SIGKILL to process 61 uid 501 "containerd": oom_score 805')
    ]


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
