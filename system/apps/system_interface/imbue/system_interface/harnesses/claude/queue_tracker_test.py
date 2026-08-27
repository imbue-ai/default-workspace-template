"""Unit tests for the Claude queued-message populator (conservation-law model).

The scenario tests feed raw JSONL lines through ``parse_queue_signals`` +
``ClaudeQueueTracker`` (exercising the parse and the routing together), covering
the model in ``docs/claude_queued_messages_impl.md``: enqueue adds a FIFO entry
(phantom for task-notifications / blank), and each dequeue / remove / popAll
record pops the FIFO head. The regression test replays the exact 12-record ledger
of the real stranded-message bug (mngr-delivered slash command + task-notifications
+ dequeue/remove commits) and asserts the snapshot ends empty. The fixture tests
replay five recorded real sessions and assert the conservation law plus a
self-correcting net-to-empty replay.
"""

import json
from pathlib import Path

from imbue.system_interface.harnesses.claude.queue_tracker import ClaudeQueueTracker
from imbue.system_interface.harnesses.claude.session_parser import parse_queue_signals

_SESSION = "sess-1"
_TESTDATA_DIR = Path(__file__).parent / "testdata"
_FIXTURE_DIR = _TESTDATA_DIR / "queue_sessions"
_STRANDED_REPRO = _TESTDATA_DIR / "stranded_queue_repro.jsonl"


def _enqueue_line(content: str, *, timestamp: str = "2026-08-07T00:00:00.000Z", session_id: str = _SESSION) -> str:
    return json.dumps(
        {
            "type": "queue-operation",
            "operation": "enqueue",
            "timestamp": timestamp,
            "sessionId": session_id,
            "content": content,
        }
    )


def _pop_all_line(content: str, *, session_id: str = _SESSION) -> str:
    return json.dumps(
        {
            "type": "queue-operation",
            "operation": "popAll",
            "timestamp": "t",
            "sessionId": session_id,
            "content": content,
        }
    )


def _dequeue_line(*, session_id: str = _SESSION) -> str:
    return json.dumps({"type": "queue-operation", "operation": "dequeue", "timestamp": "t", "sessionId": session_id})


def _remove_line(content: str, *, session_id: str = _SESSION) -> str:
    return json.dumps(
        {
            "type": "queue-operation",
            "operation": "remove",
            "timestamp": "t",
            "sessionId": session_id,
            "content": content,
        }
    )


def _queued_command_line(prompt: str, *, command_mode: str = "prompt", session_id: str = _SESSION) -> str:
    return json.dumps(
        {
            "type": "attachment",
            "uuid": "att-1",
            "timestamp": "t",
            "sessionId": session_id,
            "attachment": {"type": "queued_command", "prompt": prompt, "commandMode": command_mode, "timestamp": "t"},
        }
    )


def _queued_user_line(content: str, *, prompt_source: str | None = "queued", session_id: str = _SESSION) -> str:
    record: dict = {
        "type": "user",
        "uuid": "u-1",
        "timestamp": "t",
        "sessionId": session_id,
        "message": {"role": "user", "content": content},
    }
    if prompt_source is not None:
        record["promptSource"] = prompt_source
    return json.dumps(record)


def _feed(tracker: ClaudeQueueTracker, *lines: str) -> None:
    for line in lines:
        signal = parse_queue_signals(line)
        if signal is not None:
            tracker.consume(signal)


def _contents(tracker: ClaudeQueueTracker) -> list[str]:
    return [entry["content"] for entry in tracker.snapshot()]


# --- Scenario tests -----------------------------------------------------------


def test_enqueue_then_dequeue_nets_to_empty() -> None:
    tracker = ClaudeQueueTracker.build()
    _feed(tracker, _enqueue_line("hello"))
    assert _contents(tracker) == ["hello"]
    _feed(tracker, _dequeue_line())
    assert tracker.snapshot() == []


def test_enqueue_then_remove_nets_to_empty() -> None:
    tracker = ClaudeQueueTracker.build()
    _feed(tracker, _enqueue_line("hello"), _remove_line("hello"))
    assert tracker.snapshot() == []


def test_pop_all_pops_one_head_per_record() -> None:
    tracker = ClaudeQueueTracker.build()
    _feed(tracker, _enqueue_line("/a"), _enqueue_line("/b"), _enqueue_line("/c"))
    assert len(tracker.snapshot()) == 3
    # popAll emits one record per flushed message; each pops exactly one head.
    _feed(tracker, _pop_all_line("/a"), _pop_all_line("/b"))
    assert _contents(tracker) == ["/c"]
    _feed(tracker, _pop_all_line("/c"))
    assert tracker.snapshot() == []


def test_task_notification_enqueue_is_a_phantom_slot_that_never_surfaces() -> None:
    tracker = ClaudeQueueTracker.build()
    # A task-notification queued alongside a real human message: only the human
    # one surfaces, but the phantom holds a FIFO slot so leaves stay aligned.
    _feed(
        tracker,
        _enqueue_line("<task-notification>\n<task-id>abc</task-id>"),
        _enqueue_line("real human message"),
    )
    assert _contents(tracker) == ["real human message"]
    # The first leave pops the phantom head; the real entry survives.
    _feed(tracker, _dequeue_line())
    assert _contents(tracker) == ["real human message"]
    # The real entry's own leave then clears it.
    _feed(tracker, _dequeue_line())
    assert tracker.snapshot() == []


def test_blank_enqueue_is_a_phantom_slot() -> None:
    tracker = ClaudeQueueTracker.build()
    _feed(tracker, _enqueue_line("   "), _enqueue_line("real"))
    assert _contents(tracker) == ["real"]


def test_idle_backstop_clears_a_survivor_with_no_leave_record() -> None:
    tracker = ClaudeQueueTracker.build()
    # An enqueue whose leave never lands (interrupt / SIGKILL / crash): the entry
    # stays until the working->IDLE backstop sweeps it.
    _feed(tracker, _enqueue_line("no leave will come"))
    assert _contents(tracker) == ["no leave will come"]
    tracker.on_idle()
    assert tracker.snapshot() == []


def test_duplicate_content_two_entries_one_leave_leaves_one() -> None:
    tracker = ClaudeQueueTracker.build()
    _feed(tracker, _enqueue_line("same"), _enqueue_line("same"))
    assert len(tracker.snapshot()) == 2
    _feed(tracker, _dequeue_line())
    assert _contents(tracker) == ["same"]


def test_single_session_replay_reproduces_the_true_queue() -> None:
    # The tracker is a pure function of the one (latest) session's ledger it is
    # fed: a full replay from byte 0 nets enqueues against leaves, leaving
    # exactly the still-parked entries -- the backend-restart rebuild case.
    tracker = ClaudeQueueTracker.build()
    _feed(
        tracker,
        _enqueue_line("committed"),
        _enqueue_line("still parked"),
        _dequeue_line(),
        _enqueue_line("also parked"),
    )
    assert _contents(tracker) == ["still parked", "also parked"]


def test_reset_clears_the_queue() -> None:
    # The watcher resets the tracker when a new latest main session is
    # registered (the process restarted); the tracker itself does no session
    # discrimination -- reset just empties the set.
    tracker = ClaudeQueueTracker.build()
    _feed(tracker, _enqueue_line("residue from the dead process"))
    assert len(tracker.snapshot()) == 1
    tracker.reset()
    assert tracker.snapshot() == []


def test_user_records_and_attachments_are_not_queue_signals() -> None:
    # Resolution keys off the ledger LEAVE ops only -- never promptSource or the
    # queued_command attachment (mngr-delivered turns commit as a "typed" dequeue).
    assert parse_queue_signals(_queued_user_line("hi", prompt_source="queued")) is None
    assert parse_queue_signals(_queued_user_line("hi", prompt_source="typed")) is None
    assert parse_queue_signals(_queued_command_line("hi")) is None
    # So neither disturbs the tracked set.
    tracker = ClaudeQueueTracker.build()
    _feed(tracker, _enqueue_line("queued"))
    _feed(tracker, _queued_user_line("queued", prompt_source="queued"), _queued_command_line("queued"))
    assert _contents(tracker) == ["queued"]


def test_snapshot_ids_are_stable_across_replays() -> None:
    first = ClaudeQueueTracker.build()
    _feed(first, _enqueue_line("hello"))
    second = ClaudeQueueTracker.build()
    _feed(second, _enqueue_line("hello"))
    assert first.snapshot() == second.snapshot()
    assert first.snapshot()[0]["queued_id"] != ""


# --- Regression: the real stranded-message bug --------------------------------


def test_stranded_message_repro_nets_to_empty() -> None:
    """The exact 12-record ledger of the live bug must leave the snapshot empty.

    Under the old (promptSource / queued_command) model the mngr-delivered
    "Status check" message stayed stranded; under the conservation-law model every
    enqueue is resolved by its dequeue/remove, so the snapshot ends empty.
    """
    lines = [line for line in _STRANDED_REPRO.read_text().splitlines() if line.strip()]
    assert len(lines) == 12
    tracker = ClaudeQueueTracker.build()
    _feed(tracker, *lines)
    assert tracker.snapshot() == []


# --- Real recorded-session fixture tests --------------------------------------


def _fixture_lines(path: Path) -> list[str]:
    return [line for line in path.read_text().splitlines() if line.strip()]


def _op_counts(lines: list[str]) -> dict[str, int]:
    counts = {"enqueue": 0, "dequeue": 0, "remove": 0, "popAll": 0, "queued_command": 0}
    for line in lines:
        record = json.loads(line)
        record_type = record.get("type")
        if record_type == "queue-operation":
            counts[record["operation"]] += 1
        elif record_type == "attachment" and (record.get("attachment") or {}).get("type") == "queued_command":
            counts["queued_command"] += 1
        else:
            # user records (and anything else) are not counted here.
            pass
    return counts


def test_recorded_sessions_obey_the_conservation_law() -> None:
    fixtures = sorted(_FIXTURE_DIR.glob("*.jsonl"))
    assert len(fixtures) == 5
    for fixture in fixtures:
        counts = _op_counts(_fixture_lines(fixture))
        # enqueue = dequeue + remove + popAll -- every enqueued message leaves the
        # queue through exactly one of the other three ops.
        assert counts["enqueue"] == counts["dequeue"] + counts["remove"] + counts["popAll"], fixture.name
        # remove is 1:1 with a queued_command attachment (both modes).
        assert counts["remove"] == counts["queued_command"], fixture.name


def test_replaying_a_recorded_session_is_self_correcting_and_deterministic() -> None:
    # Replaying a whole session's ledger from the start nets every enqueue against
    # its one leave (no cursor needed). Feeding twice yields the same snapshot.
    total_still_pending = 0
    for fixture in sorted(_FIXTURE_DIR.glob("*.jsonl")):
        lines = _fixture_lines(fixture)
        first = ClaudeQueueTracker.build()
        _feed(first, *lines)
        second = ClaudeQueueTracker.build()
        _feed(second, *lines)
        assert first.snapshot() == second.snapshot(), fixture.name
        total_still_pending += len(first.snapshot())
        # Nothing surfacing is ever a task-notification.
        assert not any(entry["content"].startswith("<task-notification>") for entry in first.snapshot()), fixture.name
    # Conservation holds exactly in all five recorded sessions, so a full replay
    # nets to empty (the old promptSource/attachment model stranded one message).
    assert total_still_pending == 0


def test_idle_backstop_drains_any_recorded_session_to_empty() -> None:
    for fixture in sorted(_FIXTURE_DIR.glob("*.jsonl")):
        tracker = ClaudeQueueTracker.build()
        _feed(tracker, *_fixture_lines(fixture))
        tracker.on_idle()
        assert tracker.snapshot() == [], fixture.name
