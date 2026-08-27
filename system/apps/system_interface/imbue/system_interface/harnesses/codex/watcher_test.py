"""Tests for the codex session watcher's read paths.

The regression these exist for: the read methods used to serve only what the background
thread had already parsed, so the first request after a system-interface restart could
answer "no transcript" for a rollout sitting on disk -- and the client caches that answer
and never refetches, leaving an empty chat until a page reload. Claude's watcher cannot
lose that race because its read paths re-read from disk on every call; these assert codex
now does the same, without the thread ever running.
"""

import json
import os
import threading
import time
from pathlib import Path
from typing import Any
from typing import Callable

from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.codex.model import CODEX_STATE_RELATIVE_PATH
from imbue.system_interface.harnesses.codex.watcher import CodexSessionWatcher
from imbue.system_interface.harnesses.harness_type import HarnessType
from imbue.system_interface.harnesses.model import model_state_path
from imbue.system_interface.harnesses.model import read_model_identity

_SESSIONS_RELATIVE = Path("plugin") / "codex" / "home" / "sessions"


def _user_line(text: str, timestamp: str) -> dict[str, Any]:
    return {"timestamp": timestamp, "type": "event_msg", "payload": {"type": "user_message", "message": text}}


def _write_rollout(agent_state_dir: Path, lines: list[dict[str, Any]]) -> Path:
    """Write a rollout plus the marker naming it, exactly as a live agent would."""
    sessions_dir = agent_state_dir / _SESSIONS_RELATIVE / "2026" / "08" / "03"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    rollout = sessions_dir / "rollout-2026-08-03T00-00-00-test.jsonl"
    rollout.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    (agent_state_dir / "codex_transcript_path").write_text(str(rollout), encoding="utf-8")
    return rollout


def _build_watcher(
    agent_state_dir: Path, on_broadcast: threading.Event | None = None
) -> tuple[CodexSessionWatcher, list[dict[str, Any]]]:
    """A watcher over ``agent_state_dir``, never started, plus the events it broadcasts.

    ``on_broadcast`` (optional) is set on every broadcast, so a started-watcher test can
    wait on the real fan-out signal instead of polling with a sleep."""
    broadcast: list[dict[str, Any]] = []

    def on_events(_agent_id: str, events: list[dict[str, Any]]) -> None:
        broadcast.extend(events)
        if on_broadcast is not None:
            on_broadcast.set()

    agent_info = AgentInfo(
        id="agent-test",
        name="test",
        state="RUNNING",
        agent_state_dir=agent_state_dir,
        claude_config_dir=agent_state_dir / "unused",
        harness=HarnessType.CODEX,
    )
    watcher = CodexSessionWatcher.build(agent_info, on_events)
    return watcher, broadcast


def test_reads_serve_history_without_the_thread_having_run(tmp_path: Path) -> None:
    """The bug: a read before the loop's first pass answered empty.

    ``start()`` is deliberately never called, which is the whole point -- this is the
    state the very first request after a restart sees.
    """
    _write_rollout(
        tmp_path, [_user_line("first", "2026-08-03T00:00:01Z"), _user_line("second", "2026-08-03T00:00:02Z")]
    )
    watcher, _ = _build_watcher(tmp_path)

    assert watcher.get_total_event_count() == 2
    assert [event["content"] for event in watcher.get_tail_events(50)] == ["first", "second"]
    assert len(watcher.get_all_events()) == 2


def _turn_context_line(model: str, effort: str, timestamp: str) -> dict[str, Any]:
    return {"timestamp": timestamp, "type": "turn_context", "payload": {"model": model, "effort": effort}}


def test_effective_model_from_turn_context_is_reflected_in_the_state_file(tmp_path: Path) -> None:
    """§4b: the watcher writes the EFFECTIVE per-turn model (from turn_context) into the model-bar
    state file, preserving the ledger-owned fast bit, so a framework fallback shows in the bar."""
    state_path = model_state_path(tmp_path, CODEX_STATE_RELATIVE_PATH)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    # The ledger already mirrored the SELECTED settings (fast on).
    state_path.write_text(json.dumps({"model": "gpt-5.6-sol", "effort": "high", "fast": True}))
    # The turn actually RAN on a fallback model/effort (over-quota downgrade).
    _write_rollout(
        tmp_path,
        [
            _turn_context_line("gpt-5.2", "low", "2026-08-03T00:00:01Z"),
            _assistant_line("m-done", "done", "2026-08-03T00:00:02Z"),
        ],
    )
    watcher, _ = _build_watcher(tmp_path)
    # A read drives _refresh -> the effective-model reflection.
    watcher.get_all_events()

    identity = read_model_identity(state_path)
    assert identity is not None
    assert identity.model_id == "gpt-5.2"
    assert identity.effort == "low"
    # Fast is preserved from the ledger's selected-settings write (turn_context carries no tier).
    assert identity.fast is True


def test_effective_model_matching_the_file_writes_nothing_new(tmp_path: Path) -> None:
    """When the effective model equals what the file already holds (selected == effective), the
    watcher does not rewrite it (no churn), leaving the existing content untouched."""
    state_path = model_state_path(tmp_path, CODEX_STATE_RELATIVE_PATH)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"model": "gpt-5.6-sol", "effort": "high", "fast": True}))
    before = state_path.read_text()
    _write_rollout(
        tmp_path,
        [
            _turn_context_line("gpt-5.6-sol", "high", "2026-08-03T00:00:01Z"),
            _assistant_line("m-done", "done", "2026-08-03T00:00:02Z"),
        ],
    )
    watcher, _ = _build_watcher(tmp_path)
    watcher.get_all_events()
    assert state_path.read_text() == before


def _write_rollout_without_marker(
    agent_state_dir: Path, lines: list[dict[str, Any]], name: str = "rollout-2026-08-03T00-00-00-web.jsonl"
) -> Path:
    """Write a rollout but NOT the marker -- a web/CLI-only agent whose UserPromptSubmit hook never fired."""
    sessions_dir = agent_state_dir / _SESSIONS_RELATIVE / "2026" / "08" / "03"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    rollout = sessions_dir / name
    rollout.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return rollout


def test_resolves_newest_rollout_when_no_marker(tmp_path: Path) -> None:
    """A web-only codex agent never gets a UserPromptSubmit marker; the newest rollout is tailed anyway.

    This is the transcript fix: the app-server does not fire the ``UserPromptSubmit`` hook (which
    writes ``codex_transcript_path``) on a programmatic turn, so without the fallback the web chat
    renders empty. With one thread per daemon the newest rollout IS the live conversation.
    """
    _write_rollout_without_marker(tmp_path, [_user_line("web-first", "2026-08-03T00:00:01Z")])
    assert not (tmp_path / "codex_transcript_path").exists()
    watcher, _ = _build_watcher(tmp_path)

    assert watcher.get_total_event_count() == 1
    assert [event["content"] for event in watcher.get_tail_events(50)] == ["web-first"]


def test_marker_takes_precedence_over_newest_rollout(tmp_path: Path) -> None:
    """When the marker IS present it wins -- the deterministic TUI path is preserved."""
    marked = _write_rollout(tmp_path, [_user_line("marked", "2026-08-03T00:00:01Z")])
    # A second, newer rollout with no bearing on the marker must NOT be picked while the marker points elsewhere.
    other = _write_rollout_without_marker(
        tmp_path,
        [_user_line("unmarked-newer", "2026-08-03T00:00:09Z")],
        name="rollout-2026-08-03T00-00-09-other.jsonl",
    )
    # Force it newest by mtime.
    os.utime(other, (10**10, 10**10))
    assert (tmp_path / "codex_transcript_path").read_text().strip() == str(marked)
    watcher, _ = _build_watcher(tmp_path)

    assert [event["content"] for event in watcher.get_tail_events(50)] == ["marked"]


def test_a_read_does_not_stop_the_loop_broadcasting_those_events(tmp_path: Path) -> None:
    """Reads advance the read cursor; the emit bookmark is separate.

    Without the split, a read would consume the bytes and the loop would then have
    nothing to broadcast -- so the live stream would silently lose exactly the events a
    reader happened to pull in first. Uses an assistant line (agent output the file reader
    still owns live) rather than a user line (now suppressed live -- see the suppression test).
    """
    _write_rollout(tmp_path, [_assistant_line("m1", "only", "2026-08-03T00:00:01Z")])
    watcher, broadcast = _build_watcher(tmp_path)

    assert len(watcher.get_tail_events(50)) == 1
    assert broadcast == []

    watcher._emit_unsent()
    assert [event["text"] for event in broadcast] == ["only"]

    # Idempotent: a second pass re-broadcasts nothing.
    watcher._emit_unsent()
    assert len(broadcast) == 1


def test_live_user_turns_are_suppressed_from_the_broadcast_but_served_by_reads(tmp_path: Path) -> None:
    """Fix 1: the subscribed ledger owns the LIVE user-turn, so the file reader must NOT broadcast
    its own copy (that is the unordered second channel A3b forbids). The user-turn still lives in
    the store, so the read paths -- the hydration a page load rebuilds from -- serve it; only the
    live broadcast omits it. Agent output on the same pass is still broadcast."""
    _write_rollout(
        tmp_path,
        [
            _user_line("hi there", "2026-08-03T00:00:01Z"),
            _assistant_line("m1", "reply", "2026-08-03T00:00:02Z"),
        ],
    )
    watcher, broadcast = _build_watcher(tmp_path)
    watcher._emit_unsent()

    # The broadcast carries the agent message but NOT the user turn.
    assert [event["type"] for event in broadcast] == ["assistant_message"]
    # The reads (hydration/backfill) DO include the user turn.
    all_types = [event["type"] for event in watcher.get_all_events()]
    assert all_types == ["user_message", "assistant_message"]
    # Suppressed events are still counted-as-sent: a later pass never leaks them into the broadcast.
    watcher._emit_unsent()
    assert [event["type"] for event in broadcast] == ["assistant_message"]


def test_reads_pick_up_lines_appended_after_the_first_read(tmp_path: Path) -> None:
    """Refresh is incremental, so a later read sees appended turns without a restart."""
    rollout = _write_rollout(tmp_path, [_user_line("first", "2026-08-03T00:00:01Z")])
    watcher, _ = _build_watcher(tmp_path)
    assert watcher.get_total_event_count() == 1

    with rollout.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_user_line("second", "2026-08-03T00:00:02Z")) + "\n")

    assert watcher.get_total_event_count() == 2
    assert [event["content"] for event in watcher.get_tail_events(50)] == ["first", "second"]


def test_missing_marker_reads_empty_rather_than_raising(tmp_path: Path) -> None:
    """An agent that has not taken a turn yet has no marker; that is normal, not an error."""
    watcher, _ = _build_watcher(tmp_path)
    assert watcher.get_total_event_count() == 0
    assert watcher.get_tail_events(50) == []


def _wait_for(broadcast_signal: threading.Event, predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    """Wait until ``predicate`` holds, woken by the watcher's own broadcast signal.

    Uses the fan-out ``Event`` rather than a sleep-poll: each broadcast sets it, we
    re-check the predicate, and clear. The watch loop wakes on a watchdog event or its
    poll safety-net, so the end state is reached within a few seconds regardless."""
    deadline = time.monotonic() + timeout
    while not predicate():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        broadcast_signal.wait(timeout=remaining)
        broadcast_signal.clear()


def test_start_tails_the_rollout_via_the_shared_watcher(tmp_path: Path) -> None:
    """The started watcher broadcasts existing content and picks up an appended line.

    This is the path the shared ``PathWatcher`` now drives (it replaced the watcher's
    own thread/observer/poll loop): ``start`` emits whatever is already on disk, then a
    later append reaches the broadcast via the watch loop. Uses assistant lines -- agent
    output the file reader still broadcasts live (user turns are ledger-owned now)."""
    broadcast_signal = threading.Event()
    rollout = _write_rollout(tmp_path, [_assistant_line("m1", "first", "2026-08-03T00:00:01Z")])
    watcher, broadcast = _build_watcher(tmp_path, broadcast_signal)
    watcher.start()
    try:
        _wait_for(broadcast_signal, lambda: [event["text"] for event in broadcast] == ["first"])
        assert [event["text"] for event in broadcast] == ["first"]

        with rollout.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_assistant_line("m2", "second", "2026-08-03T00:00:02Z")) + "\n")

        _wait_for(broadcast_signal, lambda: [event["text"] for event in broadcast] == ["first", "second"])
        assert [event["text"] for event in broadcast] == ["first", "second"]
    finally:
        watcher.stop()


def test_multibyte_char_split_across_reads_is_preserved(tmp_path: Path) -> None:
    """A UTF-8 character straddling a read boundary is completed (buffered as bytes)
    before decoding, so it is not corrupted into a replacement char."""
    watcher, _ = _build_watcher(tmp_path)
    # ensure_ascii=False so the emoji is raw UTF-8 in the file, as codex writes it
    # (serde_json does not escape non-ASCII -- real rollouts carry raw multi-byte bytes).
    data = (json.dumps(_user_line("hi\U0001f389", "2026-08-03T00:00:01Z"), ensure_ascii=False) + "\n").encode("utf-8")
    # Split in the middle of the four-byte emoji, straddling the read boundary.
    split = data.index("\U0001f389".encode("utf-8")) + 2
    sessions_dir = tmp_path / _SESSIONS_RELATIVE / "2026" / "08" / "03"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    rollout = sessions_dir / "rollout-2026-08-03T00-00-00-test.jsonl"
    (tmp_path / "codex_transcript_path").write_text(str(rollout), encoding="utf-8")
    # First chunk ends mid-character with no newline: the line is incomplete, nothing yet.
    rollout.write_bytes(data[:split])
    assert watcher.get_total_event_count() == 0
    # The rest completes the character and the line.
    with rollout.open("ab") as handle:
        handle.write(data[split:])
    assert [event["content"] for event in watcher.get_tail_events(10)] == ["hi\U0001f389"]


def _assistant_line(msg_id: str, text: str, timestamp: str) -> dict[str, Any]:
    return {
        "timestamp": timestamp,
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "id": msg_id,
            "content": [{"type": "output_text", "text": text}],
        },
    }


def test_supersession_replaces_and_rebroadcasts(tmp_path: Path) -> None:
    """A re-serialised assistant message with UPDATED content (same id) replaces the
    stored copy -- not kept stale, not duplicated -- and is re-broadcast so the client
    upgrades its held copy."""
    watcher, broadcast = _build_watcher(tmp_path)
    rollout = _write_rollout(tmp_path, [_assistant_line("m1", "first", "2026-08-03T00:00:01Z")])
    watcher._emit_unsent()
    assert [e["text"] for e in broadcast if e["type"] == "assistant_message"] == ["first"]

    with rollout.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_assistant_line("m1", "first, corrected", "2026-08-03T00:00:02Z")) + "\n")
    watcher._emit_unsent()

    assistants = [e for e in watcher.get_all_events() if e["type"] == "assistant_message"]
    assert [e["text"] for e in assistants] == ["first, corrected"]
    rebroadcast = [e for e in broadcast if e["type"] == "assistant_message"]
    assert rebroadcast[-1]["text"] == "first, corrected"


def _tool_call_line(call_id: str, name: str, args: str, timestamp: str) -> dict[str, Any]:
    return {
        "timestamp": timestamp,
        "type": "response_item",
        "payload": {"type": "function_call", "call_id": call_id, "name": name, "arguments": args},
    }


def _abort_line(turn_id: str, timestamp: str) -> dict[str, Any]:
    return {"timestamp": timestamp, "type": "event_msg", "payload": {"type": "turn_aborted", "turn_id": turn_id}}


def test_interrupt_synthesizes_results_for_open_calls(tmp_path: Path) -> None:
    """A turn_aborted with an in-flight tool call (no result) gets a synthetic
    'Interrupted.' result, keyed on the id a real result would use, so the card resolves
    instead of spinning forever."""
    watcher, _ = _build_watcher(tmp_path)
    _write_rollout(
        tmp_path,
        [
            _tool_call_line("c1", "exec", '{"cmd":"sleep 999"}', "2026-08-03T00:00:01Z"),
            _abort_line("t1", "2026-08-03T00:00:02Z"),
        ],
    )
    results = [e for e in watcher.get_all_events() if e["type"] == "tool_result"]
    assert len(results) == 1
    assert results[0]["tool_call_id"] == "c1"
    assert results[0]["output"] == "Interrupted."
    assert results[0]["is_error"] is True
    assert results[0]["event_id"] == "codex-result-c1"


def test_interrupt_leaves_already_completed_calls_alone(tmp_path: Path) -> None:
    """A call that already has a real result is not given a synthetic Interrupted one."""
    watcher, _ = _build_watcher(tmp_path)
    _write_rollout(
        tmp_path,
        [
            _tool_call_line("c1", "exec", '{"cmd":"ls"}', "2026-08-03T00:00:01Z"),
            {
                "timestamp": "2026-08-03T00:00:02Z",
                "type": "response_item",
                "payload": {"type": "function_call_output", "call_id": "c1", "output": "ok"},
            },
            _abort_line("t1", "2026-08-03T00:00:03Z"),
        ],
    )
    results = [e for e in watcher.get_all_events() if e["type"] == "tool_result"]
    assert [r["output"] for r in results] == ["ok"]


def test_identical_reserialisation_is_dropped(tmp_path: Path) -> None:
    """An exact re-serialisation (same id, same content) is a pure duplicate: not
    re-broadcast and not duplicated in the store."""
    watcher, broadcast = _build_watcher(tmp_path)
    rollout = _write_rollout(tmp_path, [_assistant_line("m1", "hello", "2026-08-03T00:00:01Z")])
    watcher._emit_unsent()
    before = len(broadcast)

    with rollout.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_assistant_line("m1", "hello", "2026-08-03T00:00:01Z")) + "\n")
    watcher._emit_unsent()

    assert len(broadcast) == before
    assert len([e for e in watcher.get_all_events() if e["type"] == "assistant_message"]) == 1
