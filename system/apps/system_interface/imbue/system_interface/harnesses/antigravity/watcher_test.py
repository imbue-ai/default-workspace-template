"""Unit tests for the antigravity watcher's SQLite row-offset tailing.

Drives ``_collect_new_events`` directly (synchronously) rather than the background poll
thread, so the scan/cursor/two-phase logic is tested without timing flakiness.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from imbue.system_interface.activity_state import ACTIVE_MARKER_FILENAME
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.antigravity.queue_tracker import drop_tracker
from imbue.system_interface.harnesses.antigravity.testing import append_step
from imbue.system_interface.harnesses.antigravity.testing import build_metadata
from imbue.system_interface.harnesses.antigravity.testing import build_step_payload
from imbue.system_interface.harnesses.antigravity.testing import build_steps_db
from imbue.system_interface.harnesses.antigravity.testing import build_tool_metadata
from imbue.system_interface.harnesses.antigravity.testing import encode_varint
from imbue.system_interface.harnesses.antigravity.testing import len_field
from imbue.system_interface.harnesses.antigravity.testing import set_step_status
from imbue.system_interface.harnesses.antigravity.testing import str_field
from imbue.system_interface.harnesses.antigravity.turn_state import drop_turn_state
from imbue.system_interface.harnesses.antigravity.watcher import AntigravitySessionWatcher

_STATUS_RUNNING = 2
_STATUS_DONE = 3
_TYPE_USER = 14
_TYPE_PLANNER = 15
_TYPE_RUN_COMMAND = 21


def _user_payload(text: str) -> bytes:
    return build_step_payload(build_metadata(source=4), body=len_field(19, str_field(1, text)))


def _planner_payload(text: str) -> bytes:
    return build_step_payload(build_metadata(source=2), body=len_field(20, str_field(1, text)))


def _tool_payload(result: str = "output", *, short: str = "Running ls") -> bytes:
    metadata = build_tool_metadata("run_command", '{"CommandLine":"ls"}', short=short)
    return build_step_payload(metadata, body=len_field(28, str_field(21, result)))


def _make_watcher(tmp_path: Path, conv_ids: list[str]) -> AntigravitySessionWatcher:
    (tmp_path / "antigravity_conversation_ids").write_text("\n".join(conv_ids) + "\n")
    (tmp_path / "plugin" / "antigravity" / "home" / ".gemini" / "antigravity-cli" / "conversations").mkdir(
        parents=True, exist_ok=True
    )
    # Unique per test: the tracker and turn-state registries are keyed by agent id and live
    # for the agent's life, so a shared id leaks one test's queue into the next.
    agent_id = f"agent-{tmp_path.name}"
    drop_tracker(agent_id)
    drop_turn_state(agent_id)
    agent_info = AgentInfo(
        id=agent_id,
        name="agy-test",
        state="RUNNING",
        agent_state_dir=tmp_path,
        claude_config_dir=tmp_path,
    )
    return AntigravitySessionWatcher.build(agent_info, lambda _agent_id, _events: None)


def _conv_db_path(tmp_path: Path, conv_id: str) -> Path:
    return (
        tmp_path
        / "plugin"
        / "antigravity"
        / "home"
        / ".gemini"
        / "antigravity-cli"
        / "conversations"
        / f"{conv_id}.db"
    )


def test_scans_a_settled_conversation(tmp_path: Path) -> None:
    conv = "11111111-1111-1111-1111-111111111111"
    watcher = _make_watcher(tmp_path, [conv])
    build_steps_db(
        _conv_db_path(tmp_path, conv),
        [
            (0, _TYPE_USER, _STATUS_DONE, _user_payload("<USER_REQUEST>\nhi\n</USER_REQUEST>")),
            (1, _TYPE_RUN_COMMAND, _STATUS_DONE, _tool_payload("hello output")),
            (2, _TYPE_PLANNER, _STATUS_DONE, _planner_payload("all done")),
        ],
    )
    events = watcher._collect_new_events()
    kinds = [e["type"] for e in events]
    assert kinds == ["user_message", "assistant_message", "tool_result", "assistant_message"]
    assert events[0]["content"] == "hi"
    assert events[1]["tool_calls"][0]["caption_label"] == "Running ls"
    assert events[2]["output"] == "hello output"
    assert events[3]["text"] == "all done"


def test_second_scan_after_no_change_yields_nothing(tmp_path: Path) -> None:
    conv = "22222222-2222-2222-2222-222222222222"
    watcher = _make_watcher(tmp_path, [conv])
    build_steps_db(_conv_db_path(tmp_path, conv), [(0, _TYPE_USER, _STATUS_DONE, _user_payload("hey"))])
    assert len(watcher._collect_new_events()) == 1
    assert watcher._collect_new_events() == []


def test_running_tool_emits_call_then_result_on_settle(tmp_path: Path) -> None:
    conv = "33333333-3333-3333-3333-333333333333"
    watcher = _make_watcher(tmp_path, [conv])
    db = _conv_db_path(tmp_path, conv)
    build_steps_db(db, [(0, _TYPE_RUN_COMMAND, _STATUS_RUNNING, _tool_payload("", short="Running ls"))])

    first = watcher._collect_new_events()
    assert [e["type"] for e in first] == ["assistant_message"]
    assert first[0]["tool_calls"][0]["caption_label"] == "Running ls"

    # The step settles with its result.
    set_step_status(db, 0, _STATUS_DONE, _tool_payload("final output"))
    second = watcher._collect_new_events()
    # The call is not re-emitted; only the result is added.
    assert [e["type"] for e in second] == ["tool_result"]
    assert second[0]["output"] == "final output"
    # call and result share the id
    assert second[0]["tool_call_id"] == first[0]["tool_calls"][0]["tool_call_id"]


def test_cursor_does_not_block_later_rows_behind_a_running_one(tmp_path: Path) -> None:
    conv = "44444444-4444-4444-4444-444444444444"
    watcher = _make_watcher(tmp_path, [conv])
    db = _conv_db_path(tmp_path, conv)
    # A backgrounded run_command lingers at RUNNING while a later step settles after it.
    build_steps_db(db, [(0, _TYPE_RUN_COMMAND, _STATUS_RUNNING, _tool_payload("", short="Running server"))])
    watcher._collect_new_events()
    append_step(db, (1, _TYPE_PLANNER, _STATUS_DONE, _planner_payload("moved on")))
    events = watcher._collect_new_events()
    # The later planner is emitted even though idx 0 is still running.
    assert any(e["type"] == "assistant_message" and e.get("text") == "moved on" for e in events)


def test_truncated_row_stops_scan_until_next_pass(tmp_path: Path) -> None:
    conv = "55555555-5555-5555-5555-555555555555"
    watcher = _make_watcher(tmp_path, [conv])
    db = _conv_db_path(tmp_path, conv)
    truncated = _tag_len_overrun()
    build_steps_db(
        db,
        [
            (0, _TYPE_USER, _STATUS_DONE, _user_payload("first")),
            # idx 1 is a mid-write row the decoder rejects as truncated.
            (1, _TYPE_PLANNER, _STATUS_DONE, truncated),
            (2, _TYPE_PLANNER, _STATUS_DONE, _planner_payload("later")),
        ],
    )
    events = watcher._collect_new_events()
    # Only idx 0 emitted; the scan stops at the truncated idx 1 and never reaches idx 2.
    assert [e["type"] for e in events] == ["user_message"]
    # Once idx 1 is rewritten whole, the scan resumes through idx 2.
    set_step_status(db, 1, _STATUS_DONE, _planner_payload("fixed"))
    events2 = watcher._collect_new_events()
    assert [e.get("text") for e in events2] == ["fixed", "later"]


def test_multiple_conversations_concatenate_in_file_order(tmp_path: Path) -> None:
    conv_a = "aaaaaaaa-0000-0000-0000-000000000000"
    conv_b = "bbbbbbbb-0000-0000-0000-000000000000"
    watcher = _make_watcher(tmp_path, [conv_a, conv_b])
    build_steps_db(_conv_db_path(tmp_path, conv_a), [(0, _TYPE_USER, _STATUS_DONE, _user_payload("from A"))])
    build_steps_db(_conv_db_path(tmp_path, conv_b), [(0, _TYPE_USER, _STATUS_DONE, _user_payload("from B"))])
    events = watcher._collect_new_events()
    assert [e["content"] for e in events] == ["from A", "from B"]
    # ids are namespaced by conversation, so same-idx rows never collide
    assert events[0]["event_id"] == f"{conv_a}:0:user"
    assert events[1]["event_id"] == f"{conv_b}:0:user"


def test_paging_methods(tmp_path: Path) -> None:
    conv = "66666666-6666-6666-6666-666666666666"
    watcher = _make_watcher(tmp_path, [conv])
    build_steps_db(
        _conv_db_path(tmp_path, conv),
        [(i, _TYPE_PLANNER, _STATUS_DONE, _planner_payload(f"msg{i}")) for i in range(5)],
    )
    watcher._collect_new_events()
    assert watcher.get_total_event_count() == 5
    assert [e["text"] for e in watcher.get_tail_events(2)] == ["msg3", "msg4"]
    assert [e["text"] for e in watcher.get_events_at_offset(1, 2)] == ["msg1", "msg2"]
    third_id = watcher.get_all_events()[2]["event_id"]
    assert watcher.get_event_offset(third_id) == 2
    assert [e["text"] for e in watcher.get_backfill_events(third_id, 2)] == ["msg0", "msg1"]
    assert [e["text"] for e in watcher.get_forward_events(third_id, 1)] == ["msg3"]


def _tag_len_overrun() -> bytes:
    """A metadata (f5) length-delimited field that claims more bytes than are present -- the
    shape a row half-written by agy takes, which the decoder rejects as truncated."""
    return _tag(5, 2) + encode_varint(80) + b"too short"


def _tag(field: int, wire: int) -> bytes:
    return encode_varint((field << 3) | wire)


# --- the flush worker: the only typist ----------------------------------------------------
#
# This code path had NO tests, which is why an ungated flush -- one that typed the held queue
# into a live turn every 5 seconds -- shipped green.


def _flushing_watcher(tmp_path: Path, conv: str, sent: list[str], *, is_alive: bool = True, does_commit: bool = True):
    """A watcher whose send records the text and, like agy, commits it as a user turn.

    ``does_commit=False`` is the parked case: mngr reports the send accepted (its probe is the
    busy marker's mtime, which advances either way) but agy wrote no user row.
    """
    watcher = _make_watcher(tmp_path, [conv])
    next_idx = [100]

    def _send(text: str) -> bool:
        sent.append(text)
        if does_commit:
            append_step(_conv_db_path(tmp_path, conv), (next_idx[0], _TYPE_USER, _STATUS_DONE, _user_payload(text)))
            next_idx[0] += 1
        return True

    watcher.set_flush_hooks(_send, lambda: is_alive)
    return watcher


def test_the_flush_never_types_while_a_turn_is_open(tmp_path: Path) -> None:
    """THE bug. A message held mid-turn must not be typed into that turn: agy parks it
    invisibly, merges it into the running turn, and mngr's marker-mtime ack reports success."""
    conv = "conv-open"
    sent: list[str] = []
    watcher = _flushing_watcher(tmp_path, conv, sent)
    # A tool_result tail: agy has run a tool and is about to speak again -- mid-turn.
    build_steps_db(
        _conv_db_path(tmp_path, conv),
        [(0, _TYPE_USER, _STATUS_DONE, _user_payload("go")), (1, _TYPE_RUN_COMMAND, _STATUS_DONE, _tool_payload())],
    )
    (tmp_path / ACTIVE_MARKER_FILENAME).write_text("")
    watcher._collect_new_events()
    watcher._publish_turn_state()
    watcher.get_queued_messages()
    watcher._queue.enqueue("also fix the tests", "t0")

    watcher._attempt_flush()

    assert sent == [], "a held message must never be typed into a live turn"
    assert [e["content"] for e in watcher.get_queued_messages()] == ["also fix the tests"]


def test_the_flush_delivers_once_the_turn_is_closed(tmp_path: Path) -> None:
    conv = "conv-closed"
    sent: list[str] = []
    watcher = _flushing_watcher(tmp_path, conv, sent)
    build_steps_db(
        _conv_db_path(tmp_path, conv),
        [
            (0, _TYPE_USER, _STATUS_DONE, _user_payload("go")),
            (1, _TYPE_PLANNER, _STATUS_DONE, _planner_payload("done")),
        ],
    )
    watcher._collect_new_events()
    watcher._publish_turn_state()
    watcher._queue.enqueue("next thing", "t0")

    watcher._attempt_flush()

    assert sent == ["next thing"]


def test_a_dead_agent_has_its_queue_returned_not_destroyed(tmp_path: Path) -> None:
    """The queue is empty when an agent is stopped -- but the messages the user sent and saw
    accepted must END UP somewhere. Deleting them is the swallow wearing a different hat."""
    conv = "conv-dead"
    sent: list[str] = []
    watcher = _flushing_watcher(tmp_path, conv, sent, is_alive=False)
    build_steps_db(_conv_db_path(tmp_path, conv), [(0, _TYPE_USER, _STATUS_DONE, _user_payload("go"))])
    watcher._collect_new_events()
    watcher._publish_turn_state()
    watcher._queue.enqueue("stranded", "t0")

    watcher._attempt_flush()

    assert sent == [], "a dead agent is never typed into -- mngr's send would auto-start it"
    assert watcher.get_queued_messages() == [], "and the queue does not linger"


def test_the_flush_still_drains_after_a_cancelled_tool_chain(tmp_path: Path) -> None:
    """Measured on agy 1.1.20: a cancelled tool call settles as CANCELED and the parser emits
    a tool_result, so the tail reads 'open' forever. An unbounded gate would make the first
    stop an agent receives wedge its queue permanently -- worse than the bug it replaced."""
    conv = "conv-cancelled"
    sent: list[str] = []
    watcher = _flushing_watcher(tmp_path, conv, sent)
    build_steps_db(
        _conv_db_path(tmp_path, conv),
        [(0, _TYPE_USER, _STATUS_DONE, _user_payload("go")), (1, _TYPE_RUN_COMMAND, _STATUS_DONE, _tool_payload())],
    )
    watcher._collect_new_events()
    watcher._publish_turn_state()
    watcher._queue.enqueue("after the stop", "t0")
    # We cancelled: the tail is abandoned, not live.
    watcher.turn_state().note_cancelled()

    watcher._attempt_flush()

    assert sent == ["after the stop"], "a cancelled turn must not hold the queue forever"


def test_a_claimed_queue_is_not_flushed_twice(tmp_path: Path) -> None:
    """The claim is the mutual exclusion between the worker and the tap."""
    conv = "conv-claimed"
    sent: list[str] = []
    watcher = _flushing_watcher(tmp_path, conv, sent)
    build_steps_db(
        _conv_db_path(tmp_path, conv),
        [
            (0, _TYPE_USER, _STATUS_DONE, _user_payload("go")),
            (1, _TYPE_PLANNER, _STATUS_DONE, _planner_payload("done")),
        ],
    )
    watcher._collect_new_events()
    watcher._publish_turn_state()
    watcher._queue.enqueue("once", "t0")
    watcher.claim_queue_for_tap()

    watcher._attempt_flush()

    assert sent == [], "the tap holds the claim; the worker must not send the same block"


def test_the_queue_entry_departs_before_its_turn_arrives(tmp_path: Path) -> None:
    """Contract A3b: for a real->real transition, remove the old state BEFORE showing the new.

    The witness loop used to emit transcript events as it found them, so the committed turn
    landed on screen while the entry was still rendered "Sending..." -- one message in two
    states at once, seen live as "chat, then still queued, then it disappears".
    """
    conv = "conv-order"
    sent: list[str] = []
    order: list[str] = []

    watcher = _make_watcher(tmp_path, [conv])
    next_idx = [100]

    def _send(text: str) -> bool:
        sent.append(text)
        append_step(_conv_db_path(tmp_path, conv), (next_idx[0], _TYPE_USER, _STATUS_DONE, _user_payload(text)))
        next_idx[0] += 1
        return True

    watcher.set_flush_hooks(_send, lambda: True)
    watcher.set_queue_snapshot_callback(lambda snap: order.append("queue-empty" if not snap else "queue-present"))
    watcher._on_events = lambda _agent_id, events: order.append("transcript")

    build_steps_db(
        _conv_db_path(tmp_path, conv),
        [
            (0, _TYPE_USER, _STATUS_DONE, _user_payload("go")),
            (1, _TYPE_PLANNER, _STATUS_DONE, _planner_payload("done")),
        ],
    )
    watcher._collect_new_events()
    watcher._publish_turn_state()
    watcher._queue.attach(publish=watcher._publish_snapshot, wake=lambda: None)
    watcher._queue.enqueue("deliver me", "t0")
    order.clear()

    watcher._attempt_flush()

    assert sent == ["deliver me"]
    assert "transcript" in order, "the committed turn must still be emitted"
    assert order.index("queue-empty") < order.index("transcript"), (
        f"the entry must leave the queue BEFORE its turn appears; got {order}"
    )


def test_the_poll_loop_cannot_emit_the_turn_before_its_chip_departs(tmp_path: Path) -> None:
    """Contract A3b, against the OTHER thread -- the half the ordering test could not see.

    ``test_the_queue_entry_departs_before_its_turn_arrives`` drives ``_attempt_flush`` on a
    watcher whose poll thread was never started, so the flush worker is the only collector and
    its careful ordering always holds. Live there are two: ``_collect_new_events`` is
    destructive, so whichever thread scans the delivered row first is the one that emits it --
    and the poll thread wakes on agy's own sqlite write, so it wins essentially every time and
    ships the committed turn while the chip is still on screen. That is the "chat, then still
    queued, then the queue disappears" blip, and it survived the fix that was supposed to kill
    it.

    Interleaving is forced rather than raced: the poll tick fires from INSIDE the send, which
    is exactly when agy has written the row and the watchdog would have woken it.
    """
    conv = "conv-race"
    sent: list[str] = []
    order: list[str] = []
    emitted: list[dict[str, Any]] = []

    watcher = _make_watcher(tmp_path, [conv])
    next_idx = [100]

    def _send(text: str) -> bool:
        sent.append(text)
        append_step(_conv_db_path(tmp_path, conv), (next_idx[0], _TYPE_USER, _STATUS_DONE, _user_payload(text)))
        next_idx[0] += 1
        # The poll thread, waking on agy's write mid-send. Without the embargo this emits the
        # user turn here -- before finish_flush has removed the entry.
        watcher._poll_once()
        return True

    def _on_events(_agent_id: str, events: list[dict[str, Any]]) -> None:
        order.append("transcript")
        emitted.extend(events)

    watcher.set_flush_hooks(_send, lambda: True)
    watcher.set_queue_snapshot_callback(lambda snap: order.append("queue-empty" if not snap else "queue-present"))
    watcher._on_events = _on_events

    build_steps_db(
        _conv_db_path(tmp_path, conv),
        [
            (0, _TYPE_USER, _STATUS_DONE, _user_payload("go")),
            (1, _TYPE_PLANNER, _STATUS_DONE, _planner_payload("done")),
        ],
    )
    watcher._collect_new_events()
    watcher._publish_turn_state()
    watcher._queue.attach(publish=watcher._publish_snapshot, wake=lambda: None)
    watcher._queue.enqueue("deliver me", "t0")
    order.clear()

    watcher._attempt_flush()

    assert sent == ["deliver me"]
    assert "transcript" in order, "the committed turn must still be emitted"
    assert order.index("queue-empty") < order.index("transcript"), (
        f"the entry must leave the queue BEFORE its turn appears, whichever thread scanned it; got {order}"
    )
    # Conservation: the embargo must DELAY the row, never drop it or double it. A fix that
    # simply swallowed the poll thread's copy would satisfy the ordering assert above.
    delivered_rows = [e for e in emitted if e.get("type") == "user_message" and e.get("content") == "deliver me"]
    assert len(delivered_rows) == 1, f"the delivered turn must be emitted exactly once; got {len(delivered_rows)}"


def test_the_embargo_is_released_when_a_flush_never_witnesses_its_block(tmp_path: Path) -> None:
    """The embargo must not be able to wedge the transcript.

    It is a deadline, not a flag, precisely so a flush thread that dies mid-send cannot mute
    the poll thread forever -- but the ordinary no-delivery path must clear it immediately
    rather than leaving the transcript muted for the full ceiling.
    """
    conv = "conv-embargo"
    sent: list[str] = []
    watcher = _flushing_watcher(tmp_path, conv, sent, does_commit=False)
    # This test is about what happens AFTER the window expires; the real 15s is dead wait.
    watcher._delivery_witness_seconds = 0.1
    build_steps_db(
        _conv_db_path(tmp_path, conv),
        [
            (0, _TYPE_USER, _STATUS_DONE, _user_payload("go")),
            (1, _TYPE_PLANNER, _STATUS_DONE, _planner_payload("done")),
        ],
    )
    watcher._collect_new_events()
    watcher._publish_turn_state()
    watcher._queue.attach(publish=watcher._publish_snapshot, wake=lambda: None)
    watcher._queue.enqueue("never lands", "t0")

    watcher._attempt_flush()

    assert sent == ["never lands"], "the send was attempted"
    assert watcher._emit_embargo_until == 0.0, "a flush that witnessed nothing must still lift the embargo"

    # And the poll thread emits again straight away.
    emitted: list[str] = []
    watcher._on_events = lambda _agent_id, events: emitted.append("transcript")
    append_step(_conv_db_path(tmp_path, conv), (200, _TYPE_PLANNER, _STATUS_DONE, _planner_payload("later")))
    watcher._poll_once()
    assert emitted == ["transcript"], "the transcript must not stay muted after a failed flush"
