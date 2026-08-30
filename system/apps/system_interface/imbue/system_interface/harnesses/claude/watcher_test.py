"""Tests for the session file watcher."""

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from imbue.system_interface.harnesses.claude.watcher import ClaudeSessionWatcher


def _user_event(index: int, content: str | None = None) -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": f"uuid-{index}",
        "timestamp": f"2026-01-01T00:00:{index:02d}Z",
        "message": {"role": "user", "content": content if content is not None else f"Message {index}"},
    }


def _setup_empty_agent(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create an agent whose single session file starts empty.

    Returns (agent_state_dir, claude_config_dir, session_file).
    """
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    claude_config_dir = tmp_path / "claude_config"
    session_dir = claude_config_dir / "projects" / "hash123"
    session_dir.mkdir(parents=True)
    session_file = session_dir / "test-session.jsonl"
    session_file.write_bytes(b"")
    (agent_state_dir / "claude_session_id_history").write_text("test-session\n")
    return agent_state_dir, claude_config_dir, session_file


def _make_watcher(
    agent_state_dir: Path, claude_config_dir: Path, collected: list[dict[str, Any]]
) -> ClaudeSessionWatcher:
    return ClaudeSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda _aid, evts: collected.extend(evts),
    )


def _write_session_file(projects_dir: Path, session_id: str, events: list[dict[str, Any]]) -> Path:
    session_dir = projects_dir / "hash123"
    session_dir.mkdir(parents=True, exist_ok=True)
    session_file = session_dir / f"{session_id}.jsonl"
    content = "\n".join(json.dumps(e) for e in events) + "\n"
    session_file.write_text(content)
    return session_file


def _setup_agent(tmp_path: Path, events: list[dict[str, Any]]) -> tuple[Path, Path, str]:
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    claude_config_dir = tmp_path / "claude_config"
    projects_dir = claude_config_dir / "projects"

    session_id = "test-session"
    _write_session_file(projects_dir, session_id, events)
    (agent_state_dir / "claude_session_id_history").write_text(f"{session_id}\n")

    return agent_state_dir, claude_config_dir, session_id


def test_get_all_events_returns_parsed_events(tmp_path: Path) -> None:
    events = [
        {
            "type": "user",
            "uuid": "uuid-1",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": "user", "content": "Hello"},
        },
        {
            "type": "assistant",
            "uuid": "uuid-2",
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-6",
                "content": [{"type": "text", "text": "Hi!"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        },
    ]

    agent_state_dir, claude_config_dir, _ = _setup_agent(tmp_path, events)
    collected: list[tuple[str, list[dict[str, Any]]]] = []

    watcher = ClaudeSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: collected.append((aid, evts)),
    )

    result = watcher.get_all_events()
    assert len(result) == 2
    assert result[0]["type"] == "user_message"
    assert result[1]["type"] == "assistant_message"


def test_get_all_events_with_tail(tmp_path: Path) -> None:
    events = [
        {
            "type": "user",
            "uuid": f"uuid-{i}",
            "timestamp": f"2026-01-01T00:00:{i:02d}Z",
            "message": {"role": "user", "content": f"Message {i}"},
        }
        for i in range(10)
    ]

    agent_state_dir, claude_config_dir, _ = _setup_agent(tmp_path, events)

    watcher = ClaudeSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: None,
    )

    result = watcher.get_all_events()
    assert len(result) == 10
    assert result[0]["content"] == "Message 0"
    assert result[9]["content"] == "Message 9"


def test_get_backfill_events(tmp_path: Path) -> None:
    events = [
        {
            "type": "user",
            "uuid": f"uuid-{i}",
            "timestamp": f"2026-01-01T00:00:{i:02d}Z",
            "message": {"role": "user", "content": f"Message {i}"},
        }
        for i in range(10)
    ]

    agent_state_dir, claude_config_dir, _ = _setup_agent(tmp_path, events)

    watcher = ClaudeSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: None,
    )

    # Get events before uuid-5-user
    result = watcher.get_backfill_events("uuid-5-user", limit=3)
    assert len(result) == 3
    assert result[0]["content"] == "Message 2"
    assert result[2]["content"] == "Message 4"


def test_watcher_detects_new_events(tmp_path: Path) -> None:
    events = [
        {
            "type": "user",
            "uuid": "uuid-1",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": "user", "content": "Hello"},
        },
    ]

    agent_state_dir, claude_config_dir, session_id = _setup_agent(tmp_path, events)
    collected: list[tuple[str, list[dict[str, Any]]]] = []

    watcher = ClaudeSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: collected.append((aid, evts)),
    )

    # Load initial events (this sets the byte offsets)
    initial = watcher.get_all_events()
    assert len(initial) == 1

    # Start the watcher and give it time to initialize
    watcher.start()
    time.sleep(2.0)  # Allow watcher to fully initialize and set offsets

    try:
        # Append a new event to the session file
        session_file = claude_config_dir / "projects" / "hash123" / f"{session_id}.jsonl"
        new_event = {
            "type": "assistant",
            "uuid": "uuid-2",
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-6",
                "content": [{"type": "text", "text": "Hi!"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        }
        with open(session_file, "a") as f:
            f.write(json.dumps(new_event) + "\n")

        # Wait for the watcher to pick it up
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if collected:
                break
            time.sleep(0.2)

        assert len(collected) >= 1, "Watcher did not detect new events"
        assert collected[0][0] == "test-agent"
        assert collected[0][1][0]["type"] == "assistant_message"
    finally:
        watcher.stop()


def test_watcher_handles_missing_history_file(tmp_path: Path) -> None:
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    claude_config_dir = tmp_path / "claude_config"

    watcher = ClaudeSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: None,
    )

    # Should not raise
    result = watcher.get_all_events()
    assert len(result) == 0


def test_poll_does_not_lose_record_split_mid_line(tmp_path: Path) -> None:
    """A read landing mid-line must not lose the partial record (issue A)."""
    agent_state_dir, claude_config_dir, session_file = _setup_empty_agent(tmp_path)
    collected: list[dict[str, Any]] = []
    watcher = _make_watcher(agent_state_dir, claude_config_dir, collected)

    line1 = (json.dumps(_user_event(0)) + "\n").encode("utf-8")
    line2 = (json.dumps(_user_event(1)) + "\n").encode("utf-8")

    # Write line1 fully plus only the first half of line2 (no terminating newline).
    with open(session_file, "ab") as f:
        f.write(line1)
        f.write(line2[: len(line2) // 2])
    watcher._emit_cycle()

    # Only the complete record is emitted; the partial line is retained, not lost.
    assert [e["event_id"] for e in collected] == ["uuid-0-user"]

    # Flush the rest of line2; the previously-partial record must now appear.
    with open(session_file, "ab") as f:
        f.write(line2[len(line2) // 2 :])
    watcher._emit_cycle()

    assert [e["event_id"] for e in collected] == ["uuid-0-user", "uuid-1-user"]
    assert collected[1]["content"] == "Message 1"


def test_poll_does_not_corrupt_split_multibyte_utf8(tmp_path: Path) -> None:
    """A read splitting a multi-byte UTF-8 sequence must not corrupt it (issue A)."""
    agent_state_dir, claude_config_dir, session_file = _setup_empty_agent(tmp_path)
    collected: list[dict[str, Any]] = []
    watcher = _make_watcher(agent_state_dir, claude_config_dir, collected)

    # Content ends with a 4-byte emoji whose UTF-8 sequence we deliberately split.
    content = "café\U0001f389"
    line_bytes = (json.dumps(_user_event(0, content=content), ensure_ascii=False) + "\n").encode("utf-8")
    emoji_bytes = "\U0001f389".encode("utf-8")
    # Land inside the 4-byte sequence.
    split = line_bytes.index(emoji_bytes) + 2

    with open(session_file, "ab") as f:
        f.write(line_bytes[:split])
    watcher._emit_cycle()
    # The split multi-byte sequence is not yet complete: nothing emitted, no crash.
    assert collected == []

    with open(session_file, "ab") as f:
        f.write(line_bytes[split:])
    watcher._emit_cycle()

    assert len(collected) == 1
    assert collected[0]["content"] == content


def test_poll_emits_final_record_without_trailing_newline(tmp_path: Path) -> None:
    """A complete final record lacking a trailing newline must still be emitted (issue A)."""
    agent_state_dir, claude_config_dir, session_file = _setup_empty_agent(tmp_path)
    collected: list[dict[str, Any]] = []
    watcher = _make_watcher(agent_state_dir, claude_config_dir, collected)

    with open(session_file, "ab") as f:
        # Deliberately omit the trailing newline.
        f.write(json.dumps(_user_event(0)).encode("utf-8"))
    watcher._emit_cycle()

    assert [e["event_id"] for e in collected] == ["uuid-0-user"]


def test_poll_handles_truncation(tmp_path: Path) -> None:
    """A truncated/rotated file must be re-read from the start (issue B)."""
    agent_state_dir, claude_config_dir, session_file = _setup_empty_agent(tmp_path)
    collected: list[dict[str, Any]] = []
    watcher = _make_watcher(agent_state_dir, claude_config_dir, collected)

    with open(session_file, "ab") as f:
        f.write((json.dumps(_user_event(5)) + "\n").encode("utf-8"))
        f.write((json.dumps(_user_event(6)) + "\n").encode("utf-8"))
    watcher._emit_cycle()
    assert [e["event_id"] for e in collected] == ["uuid-5-user", "uuid-6-user"]

    # Truncate and rewrite with a shorter, different content. The new file is
    # smaller than the consumed offset; without truncation handling this would
    # be silently ignored.
    session_file.write_bytes((json.dumps(_user_event(1)) + "\n").encode("utf-8"))
    watcher._emit_cycle()

    assert "uuid-1-user" in [e["event_id"] for e in collected]


def test_poll_re_reads_truncated_file_with_recurring_event_ids(tmp_path: Path) -> None:
    """A truncate-then-rewrite that reuses prior event IDs must re-emit them.

    The agent-wide dedup set retains every event ID it has seen. If the
    truncation reset does not purge the truncated file's IDs, the re-read is
    deduplicated against the stale IDs and silently drops every recurring
    record -- the typical atomic save-rewrite case (issue B follow-up).
    """
    agent_state_dir, claude_config_dir, session_file = _setup_empty_agent(tmp_path)
    collected: list[dict[str, Any]] = []
    watcher = _make_watcher(agent_state_dir, claude_config_dir, collected)

    original = (json.dumps(_user_event(0)) + "\n").encode("utf-8") + (json.dumps(_user_event(1)) + "\n").encode(
        "utf-8"
    )
    session_file.write_bytes(original)
    watcher._emit_cycle()
    assert [e["event_id"] for e in collected] == ["uuid-0-user", "uuid-1-user"]

    # Rewrite the file shorter but reusing event 0's ID, then growing again to
    # the same two records. The first record's ID recurs and must reappear.
    session_file.write_bytes((json.dumps(_user_event(0)) + "\n").encode("utf-8"))
    watcher._emit_cycle()
    session_file.write_bytes(original)
    watcher._emit_cycle()

    assert [e["event_id"] for e in watcher.get_all_events()] == ["uuid-0-user", "uuid-1-user"]


def test_poll_still_emits_events_parsed_by_a_concurrent_get_all_events(tmp_path: Path) -> None:
    """A concurrent HTTP read must not rob the poll loop of events to emit.

    ``get_all_events`` and the poll loop share the per-file cache offset. If
    emission were driven by what the poll's own parse produced, an
    interleaved ``get_all_events`` that parsed the new tail first would leave
    the poll loop with nothing to emit, and connected SSE clients (which never
    re-fetch) would permanently miss the event. Emission is instead driven by
    ``emitted_count``, so the poll loop still delivers the event exactly once.
    """
    agent_state_dir, claude_config_dir, session_file = _setup_empty_agent(tmp_path)
    collected: list[dict[str, Any]] = []
    watcher = _make_watcher(agent_state_dir, claude_config_dir, collected)

    with open(session_file, "ab") as f:
        f.write((json.dumps(_user_event(0)) + "\n").encode("utf-8"))

    # Simulate the HTTP path parsing the new tail into the shared cache before
    # the poll loop gets to it.
    watcher.get_all_events()

    watcher._emit_cycle()
    assert [e["event_id"] for e in collected] == ["uuid-0-user"]

    # A second poll with no new bytes must not re-emit the same event.
    watcher._emit_cycle()
    assert [e["event_id"] for e in collected] == ["uuid-0-user"]


def test_get_all_events_caches_parsed_events(tmp_path: Path) -> None:
    """Unchanged files are not re-parsed across calls (issue D)."""
    agent_state_dir, claude_config_dir, _ = _setup_agent(tmp_path, [_user_event(i) for i in range(5)])
    watcher = ClaudeSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: None,
    )

    first = watcher.get_all_events()
    second = watcher.get_all_events()

    # Re-parsing would produce fresh dict objects; cached events share identity.
    assert len(first) == len(second) == 5
    for a, b in zip(first, second, strict=True):
        assert a is b


def test_get_all_events_parses_only_new_tail(tmp_path: Path) -> None:
    """Appending to a file only parses the new tail, reusing cached events (issue D)."""
    agent_state_dir, claude_config_dir, session_id = _setup_agent(tmp_path, [_user_event(i) for i in range(3)])
    watcher = ClaudeSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: None,
    )

    first = watcher.get_all_events()
    assert len(first) == 3

    session_file = claude_config_dir / "projects" / "hash123" / f"{session_id}.jsonl"
    with open(session_file, "a") as f:
        f.write(json.dumps(_user_event(3)) + "\n")

    second = watcher.get_all_events()
    assert len(second) == 4
    # The original three events are reused (same identity), not re-parsed.
    for a, b in zip(first, second[:3], strict=True):
        assert a is b


def test_concurrent_reads_and_discovery_do_not_raise(tmp_path: Path) -> None:
    """Concurrent get_all_events + session discovery must not raise (issue C).

    Without locking, iterating _session_states while another thread inserts into
    it raises ``RuntimeError: dictionary changed size during iteration``.
    """
    agent_state_dir, claude_config_dir, _ = _setup_agent(tmp_path, [_user_event(0)])
    projects_dir = claude_config_dir / "projects"
    history_file = agent_state_dir / "claude_session_id_history"
    watcher = ClaudeSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: None,
    )

    errors: list[RuntimeError] = []
    stop = threading.Event()
    discovery_rounds = 60

    def reader() -> None:
        while not stop.is_set():
            try:
                watcher.get_all_events()
            except RuntimeError as e:
                # "dictionary changed size during iteration" is the unlocked failure.
                errors.append(e)

    def discoverer() -> None:
        try:
            for i in range(discovery_rounds):
                session_id = f"extra-session-{i}"
                _write_session_file(projects_dir, session_id, [_user_event(i)])
                with open(history_file, "a") as f:
                    f.write(f"{session_id}\n")
                watcher._emit_cycle()
        finally:
            stop.set()

    reader_thread = threading.Thread(target=reader)
    discoverer_thread = threading.Thread(target=discoverer)
    reader_thread.start()
    discoverer_thread.start()
    discoverer_thread.join(timeout=30.0)
    reader_thread.join(timeout=30.0)

    assert errors == [], f"Concurrent access raised: {errors!r}"


def test_prime_marks_backlog_emitted(tmp_path: Path) -> None:
    """Priming parses the whole backlog and marks it emitted in one lock hold, so
    the poll loop never re-broadcasts the backlog while still emitting events
    appended after start (the backlog reaches clients via the REST tail path).
    """
    agent_state_dir, claude_config_dir, session_file = _setup_empty_agent(tmp_path)
    with open(session_file, "ab") as f:
        f.write((json.dumps(_user_event(0)) + "\n").encode("utf-8"))
        f.write((json.dumps(_user_event(1)) + "\n").encode("utf-8"))

    collected: list[dict[str, Any]] = []
    watcher = _make_watcher(agent_state_dir, claude_config_dir, collected)
    watcher._prime()

    # The whole backlog is resident and marked emitted, so the poll loop has
    # nothing to broadcast for it.
    assert [e["event_id"] for e in watcher.get_all_events()] == ["uuid-0-user", "uuid-1-user"]
    watcher._emit_cycle()
    assert collected == []

    # An event appended after priming is still emitted exactly once.
    with open(session_file, "ab") as f:
        f.write((json.dumps(_user_event(2)) + "\n").encode("utf-8"))
    watcher._emit_cycle()
    assert [e["event_id"] for e in collected] == ["uuid-2-user"]


def _make_agent_tool_use_assistant(
    uuid: str,
    timestamp: str,
    tool_use_id: str,
    description: str,
    prompt: str = "do a thing",
    subagent_type: str = "Explore",
    extra_tool_uses: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = [
        {
            "type": "tool_use",
            "id": tool_use_id,
            "name": "Agent",
            "input": {"description": description, "prompt": prompt, "subagent_type": subagent_type},
        }
    ]
    if extra_tool_uses:
        content.extend(extra_tool_uses)
    return {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": timestamp,
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-7",
            "content": content,
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    }


def _write_subagent_session(
    parent_session_file: Path,
    agent_id: str,
    tool_use_id: str,
    first_timestamp: str,
    *,
    agent_type: str = "Explore",
    description: str = "test sub",
) -> Path:
    """Write a subagent jsonl + meta.json mirroring real Claude Code output.

    The jsonl first line has parentUuid=None and no sourceToolAssistantUUID (as real
    sidechain sessions do); the parent linkage lives in the meta.json `toolUseId`, which
    names the parent Agent tool_use directly.
    """
    subagents_dir = parent_session_file.parent / parent_session_file.stem / "subagents"
    subagents_dir.mkdir(parents=True, exist_ok=True)
    sub_id = f"agent-{agent_id}"
    sub_file = subagents_dir / f"{sub_id}.jsonl"
    first_line = {
        "parentUuid": None,
        "isSidechain": True,
        "agentId": agent_id,
        "type": "user",
        "message": {"role": "user", "content": "the prompt"},
        "uuid": f"sub-first-{agent_id}",
        "timestamp": first_timestamp,
        "sessionId": parent_session_file.stem,
    }
    sub_file.write_text(json.dumps(first_line) + "\n")
    meta = {"agentType": agent_type, "description": description, "toolUseId": tool_use_id}
    (subagents_dir / f"{sub_id}.meta.json").write_text(json.dumps(meta))
    return sub_file


def test_running_subagent_gets_rich_card_from_disk_linkage(tmp_path: Path) -> None:
    """A subagent that has started but not yet returned a tool_result should still
    get subagent_metadata attached to its parent Agent tool_use, sourced from the
    subagent meta.json's toolUseId."""
    parent_assistant_uuid = "assistant-uuid-1"
    parent_events: list[dict[str, Any]] = [
        _make_agent_tool_use_assistant(
            uuid=parent_assistant_uuid,
            timestamp="2026-01-01T00:00:01Z",
            tool_use_id="toolu_running",
            description="explore foo",
        ),
    ]

    agent_state_dir, claude_config_dir, session_id = _setup_agent(tmp_path, parent_events)
    parent_session_file = claude_config_dir / "projects" / "hash123" / f"{session_id}.jsonl"
    _write_subagent_session(
        parent_session_file,
        agent_id="abc123running",
        tool_use_id="toolu_running",
        first_timestamp="2026-01-01T00:00:02Z",
        agent_type="Explore",
        description="explore foo",
    )

    watcher = ClaudeSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: None,
    )

    events = watcher.get_all_events()
    assistant = next(e for e in events if e["type"] == "assistant_message")
    agent_tc = next(tc for tc in assistant["tool_calls"] if tc["tool_name"] == "Agent")
    assert "subagent_metadata" in agent_tc
    assert agent_tc["subagent_metadata"]["agent_type"] == "Explore"
    assert agent_tc["subagent_metadata"]["description"] == "explore foo"


def test_multiple_agent_tool_uses_link_to_their_subagents(tmp_path: Path) -> None:
    """When one assistant message contains multiple Agent tool_uses, each subagent's
    meta.json toolUseId links it to its specific parent tool_use, regardless of order."""
    parent_assistant_uuid = "assistant-uuid-multi"
    extra: list[dict[str, Any]] = [
        {
            "type": "tool_use",
            "id": "toolu_second",
            "name": "Agent",
            "input": {"description": "second sub", "prompt": "p2", "subagent_type": "Explore"},
        }
    ]
    parent_events: list[dict[str, Any]] = [
        _make_agent_tool_use_assistant(
            uuid=parent_assistant_uuid,
            timestamp="2026-01-01T00:00:01Z",
            tool_use_id="toolu_first",
            description="first sub",
            extra_tool_uses=extra,
        ),
    ]

    agent_state_dir, claude_config_dir, session_id = _setup_agent(tmp_path, parent_events)
    parent_session_file = claude_config_dir / "projects" / "hash123" / f"{session_id}.jsonl"
    # Deliberately link the SECOND tool_use first to prove ordering is irrelevant.
    _write_subagent_session(
        parent_session_file,
        agent_id="bbbbbsecond",
        tool_use_id="toolu_second",
        first_timestamp="2026-01-01T00:00:02Z",
        agent_type="Explore",
        description="second sub",
    )
    _write_subagent_session(
        parent_session_file,
        agent_id="aaaaafirst",
        tool_use_id="toolu_first",
        first_timestamp="2026-01-01T00:00:03Z",
        agent_type="Explore",
        description="first sub",
    )

    watcher = ClaudeSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: None,
    )

    events = watcher.get_all_events()
    assistant = next(e for e in events if e["type"] == "assistant_message")
    agent_tcs = [tc for tc in assistant["tool_calls"] if tc["tool_name"] == "Agent"]
    assert len(agent_tcs) == 2
    assert agent_tcs[0]["subagent_metadata"]["description"] == "first sub"
    assert agent_tcs[1]["subagent_metadata"]["description"] == "second sub"


def test_falls_back_to_tool_result_linkage_when_subagent_file_absent(tmp_path: Path) -> None:
    """If the subagent file is gone (older session, cleanup), the existing
    tool_result-based linkage should still resolve the metadata when the
    metadata cache happens to be populated."""
    parent_assistant_uuid = "assistant-uuid-historical"
    parent_events: list[dict[str, Any]] = [
        _make_agent_tool_use_assistant(
            uuid=parent_assistant_uuid,
            timestamp="2026-01-01T00:00:01Z",
            tool_use_id="toolu_historical",
            description="legacy sub",
        ),
        {
            "type": "user",
            "uuid": "user-uuid-tr",
            "timestamp": "2026-01-01T00:00:05Z",
            "toolUseResult": {"status": "completed", "agentId": "historicalid"},
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_historical",
                        "content": "done",
                        "is_error": False,
                    }
                ],
            },
        },
    ]

    agent_state_dir, claude_config_dir, _ = _setup_agent(tmp_path, parent_events)
    watcher = ClaudeSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: None,
    )
    # Seed the metadata cache as if the subagent file once existed but is now gone.
    watcher._subagent_metadata["agent-historicalid"] = {
        "agent_type": "Explore",
        "description": "legacy sub",
        "session_id": "agent-historicalid",
    }

    events = watcher.get_all_events()
    assistant = next(e for e in events if e["type"] == "assistant_message")
    agent_tc = next(tc for tc in assistant["tool_calls"] if tc["tool_name"] == "Agent")
    assert "subagent_metadata" in agent_tc
    assert agent_tc["subagent_metadata"]["description"] == "legacy sub"


def _latest_agent_tool_call(
    collected: list[tuple[str, list[dict[str, Any]]]], parent_uuid: str, tool_use_id: str
) -> dict[str, Any] | None:
    """Return the Agent tool_call dict for the given parent/tool_use across all emissions.

    Re-broadcasts mutate the same event object in place, so the latest view of a
    tool_call reflects whether subagent_metadata has been attached yet.
    """
    found: dict[str, Any] | None = None
    for _agent_id, events in collected:
        for event in events:
            if event.get("type") != "assistant_message" or event.get("message_uuid") != parent_uuid:
                continue
            for tc in event.get("tool_calls", []):
                if tc.get("tool_name") == "Agent" and tc.get("tool_call_id") == tool_use_id:
                    found = tc
    return found


def test_late_subagent_discovery_rebroadcasts_enriched_parent(tmp_path: Path) -> None:
    """Reproduces the live-streaming gap: a parent Agent tool_call is broadcast
    before its subagent jsonl exists, so it goes out without subagent_metadata.
    Once the subagent jsonl appears on a later discovery cycle, the parent must
    be re-broadcast carrying the rich-card metadata."""
    parent_assistant_uuid = "assistant-uuid-late"
    tool_use_id = "toolu_late"
    parent_event = _make_agent_tool_use_assistant(
        uuid=parent_assistant_uuid,
        timestamp="2026-01-01T00:00:01Z",
        tool_use_id=tool_use_id,
        description="explore late",
    )

    # Start with an empty main session so the parent line arrives *after* the
    # watcher has set its read offset -- exactly the streaming sequence.
    agent_state_dir, claude_config_dir, session_id = _setup_agent(tmp_path, [])
    parent_session_file = claude_config_dir / "projects" / "hash123" / f"{session_id}.jsonl"

    collected: list[tuple[str, list[dict[str, Any]]]] = []
    watcher = ClaudeSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: collected.append((aid, evts)),
    )

    watcher._prime()

    # The main agent writes the assistant message containing the Agent tool_call.
    with open(parent_session_file, "a") as f:
        f.write(json.dumps(parent_event) + "\n")

    watcher._emit_cycle()
    broadcast_tc = _latest_agent_tool_call(collected, parent_assistant_uuid, tool_use_id)
    assert broadcast_tc is not None, "parent assistant message should have been broadcast"
    assert "subagent_metadata" not in broadcast_tc, "no metadata before the subagent exists"

    emissions_before = len(collected)

    # The subagent process now spawns and writes its first jsonl line.
    _write_subagent_session(
        parent_session_file,
        agent_id="latesubid",
        tool_use_id=tool_use_id,
        first_timestamp="2026-01-01T00:00:02Z",
        agent_type="Explore",
        description="explore late",
    )

    watcher._emit_cycle()

    assert len(collected) == emissions_before + 1, "parent should be re-broadcast once linkage lands"
    relinked_tc = _latest_agent_tool_call(collected, parent_assistant_uuid, tool_use_id)
    assert relinked_tc is not None
    assert relinked_tc["subagent_metadata"]["agent_type"] == "Explore"
    assert relinked_tc["subagent_metadata"]["description"] == "explore late"

    # Idempotent: a fully-linked parent is not re-broadcast again.
    emissions_after_relink = len(collected)
    watcher._emit_cycle()
    assert len(collected) == emissions_after_relink


def test_inorder_subagent_discovery_does_not_rebroadcast(tmp_path: Path) -> None:
    """When the subagent jsonl already exists by the time the parent is polled,
    the parent is broadcast with metadata directly and there is nothing to
    re-broadcast."""
    parent_assistant_uuid = "assistant-uuid-inorder"
    tool_use_id = "toolu_inorder"
    parent_event = _make_agent_tool_use_assistant(
        uuid=parent_assistant_uuid,
        timestamp="2026-01-01T00:00:01Z",
        tool_use_id=tool_use_id,
        description="explore inorder",
    )

    agent_state_dir, claude_config_dir, session_id = _setup_agent(tmp_path, [])
    parent_session_file = claude_config_dir / "projects" / "hash123" / f"{session_id}.jsonl"

    collected: list[tuple[str, list[dict[str, Any]]]] = []
    watcher = ClaudeSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: collected.append((aid, evts)),
    )

    watcher._prime()

    # Subagent linkage is known before the parent line is read.
    _write_subagent_session(
        parent_session_file,
        agent_id="inordersubid",
        tool_use_id=tool_use_id,
        first_timestamp="2026-01-01T00:00:02Z",
        agent_type="Explore",
        description="explore inorder",
    )
    watcher._emit_cycle()

    with open(parent_session_file, "a") as f:
        f.write(json.dumps(parent_event) + "\n")
    watcher._emit_cycle()

    broadcast_tc = _latest_agent_tool_call(collected, parent_assistant_uuid, tool_use_id)
    assert broadcast_tc is not None
    assert "subagent_metadata" in broadcast_tc, "metadata present on first broadcast"

    emissions_before = len(collected)
    watcher._emit_cycle()
    assert len(collected) == emissions_before, "nothing left to re-broadcast"


def test_tool_result_in_later_poll_relinks_cached_parent(tmp_path: Path) -> None:
    """On Claude Code versions whose meta.json omits toolUseId, a parent Agent tool_call
    broadcast before its subagent finishes must still upgrade to the rich card the moment
    the subagent's tool_result lands in a LATER poll cycle -- not only on a page refresh.

    This exercises the persistent tool_result linkage: the parent (cycle A) and its
    tool_result (cycle B) never share a poll batch, so the rebroadcast pass must resolve
    the cached parent against the accumulated tool_call_id -> subagent_id map."""
    parent_assistant_uuid = "assistant-uuid-tr"
    tool_use_id = "toolu_tr"
    parent_event = _make_agent_tool_use_assistant(
        uuid=parent_assistant_uuid,
        timestamp="2026-01-01T00:00:01Z",
        tool_use_id=tool_use_id,
        description="explore tr",
    )
    tool_result_line: dict[str, Any] = {
        "type": "user",
        "uuid": "user-uuid-tr",
        "timestamp": "2026-01-01T00:00:09Z",
        "toolUseResult": {"status": "completed", "agentId": "trsubid"},
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": "done", "is_error": False}],
        },
    }

    agent_state_dir, claude_config_dir, session_id = _setup_agent(tmp_path, [])
    parent_session_file = claude_config_dir / "projects" / "hash123" / f"{session_id}.jsonl"

    collected: list[tuple[str, list[dict[str, Any]]]] = []
    watcher = ClaudeSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: collected.append((aid, evts)),
    )

    watcher._prime()

    # The subagent's meta.json was discovered (so its agent_type/description are known) but
    # carries no toolUseId on this version (older Claude Code), so only the tool_result
    # agentId can link it.
    watcher._subagent_metadata["agent-trsubid"] = {
        "agent_type": "Explore",
        "description": "explore tr",
        "session_id": "agent-trsubid",
    }

    # Cycle A: the parent assistant message arrives and is broadcast without metadata.
    with open(parent_session_file, "a") as f:
        f.write(json.dumps(parent_event) + "\n")
    watcher._emit_cycle()
    watcher._emit_cycle()
    broadcast_tc = _latest_agent_tool_call(collected, parent_assistant_uuid, tool_use_id)
    assert broadcast_tc is not None
    assert "subagent_metadata" not in broadcast_tc, "no metadata while the subagent is still running"

    emissions_before = len(collected)

    # Cycle B (later): the subagent finishes and its tool_result lands in a separate batch.
    with open(parent_session_file, "a") as f:
        f.write(json.dumps(tool_result_line) + "\n")
    watcher._emit_cycle()
    watcher._emit_cycle()

    assert len(collected) > emissions_before, "parent should be re-broadcast once the tool_result lands"
    relinked_tc = _latest_agent_tool_call(collected, parent_assistant_uuid, tool_use_id)
    assert relinked_tc is not None
    assert relinked_tc["subagent_metadata"]["description"] == "explore tr"


def test_parent_already_on_disk_at_start_upgrades_card_when_subagent_links(tmp_path: Path) -> None:
    """Conversation opened mid-run: the parent Agent tool_call is already on disk when the
    watcher starts, so priming marks it emitted and the poll loop never re-surfaces it. The
    card must still upgrade live once the subagent links -- the prime-time seed keeps the
    parent eligible for re-broadcast -- rather than staying on "Running..." until a refresh.

    Reproduces the most common real-world trigger: a user clicks into a conversation to watch
    a subagent that was already spawned before they opened it.
    """
    parent_assistant_uuid = "assistant-uuid-midrun"
    tool_use_id = "toolu_midrun"
    parent_event = _make_agent_tool_use_assistant(
        uuid=parent_assistant_uuid,
        timestamp="2026-01-01T00:00:01Z",
        tool_use_id=tool_use_id,
        description="explore midrun",
    )

    # The parent is already on disk before the watcher starts; the subagent does not exist yet.
    agent_state_dir, claude_config_dir, session_id = _setup_agent(tmp_path, [parent_event])
    parent_session_file = claude_config_dir / "projects" / "hash123" / f"{session_id}.jsonl"

    collected: list[tuple[str, list[dict[str, Any]]]] = []
    watcher = ClaudeSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: collected.append((aid, evts)),
    )

    watcher._prime()

    # Priming does not broadcast the backlog, and a poll re-surfaces nothing (it was marked
    # emitted), so without the prime-time seed there would be nothing left to upgrade.
    watcher._emit_cycle()
    assert _latest_agent_tool_call(collected, parent_assistant_uuid, tool_use_id) is None

    # The subagent appears while still running (meta.json present, no tool_result yet).
    _write_subagent_session(
        parent_session_file,
        agent_id="midrunsubid",
        tool_use_id=tool_use_id,
        first_timestamp="2026-01-01T00:00:02Z",
        agent_type="general-purpose",
        description="explore midrun",
    )
    watcher._emit_cycle()

    upgraded = _latest_agent_tool_call(collected, parent_assistant_uuid, tool_use_id)
    assert upgraded is not None
    assert upgraded["subagent_metadata"]["session_id"] == "agent-midrunsubid"


def test_tool_result_before_meta_discovery_does_not_strand_card(tmp_path: Path) -> None:
    """The subagent's tool_result is polled before its meta.json is discovered. The parent
    must not be dropped from the re-broadcast cache on bare linkage: it has to stay cached
    until the metadata is actually attached, then upgrade live. Evicting on bare linkage
    (a tool_call_id appearing in a linkage map) stranded the card on "Running..." until a
    page refresh, because the metadata it needed had not been discovered yet.
    """
    parent_assistant_uuid = "assistant-uuid-race"
    tool_use_id = "toolu_race"
    parent_event = _make_agent_tool_use_assistant(
        uuid=parent_assistant_uuid,
        timestamp="2026-01-01T00:00:01Z",
        tool_use_id=tool_use_id,
        description="explore race",
    )
    tool_result_line: dict[str, Any] = {
        "type": "user",
        "uuid": "user-uuid-race",
        "timestamp": "2026-01-01T00:00:05Z",
        "toolUseResult": {"status": "completed", "agentId": "racesubid"},
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": "done", "is_error": False}],
        },
    }

    agent_state_dir, claude_config_dir, session_id = _setup_agent(tmp_path, [])
    parent_session_file = claude_config_dir / "projects" / "hash123" / f"{session_id}.jsonl"

    collected: list[tuple[str, list[dict[str, Any]]]] = []
    watcher = ClaudeSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: collected.append((aid, evts)),
    )

    watcher._prime()

    # Cycle A: the parent is broadcast before any linkage exists, and cached.
    with open(parent_session_file, "a") as f:
        f.write(json.dumps(parent_event) + "\n")
    watcher._emit_cycle()
    watcher._emit_cycle()
    cycle_a_tc = _latest_agent_tool_call(collected, parent_assistant_uuid, tool_use_id)
    assert cycle_a_tc is not None
    assert "subagent_metadata" not in cycle_a_tc

    # Cycle B: the subagent finishes -- its tool_result lands -- but its meta.json has not
    # been discovered yet. The parent must remain cached, NOT be evicted on bare linkage.
    with open(parent_session_file, "a") as f:
        f.write(json.dumps(tool_result_line) + "\n")
    watcher._emit_cycle()
    watcher._emit_cycle()
    assert f"{parent_assistant_uuid}-assistant" in watcher._pending_enrichment_ids
    cycle_b_tc = _latest_agent_tool_call(collected, parent_assistant_uuid, tool_use_id)
    assert cycle_b_tc is not None
    assert "subagent_metadata" not in cycle_b_tc

    # Cycle C: the subagent's files are finally discovered; the card upgrades live.
    _write_subagent_session(
        parent_session_file,
        agent_id="racesubid",
        tool_use_id=tool_use_id,
        first_timestamp="2026-01-01T00:00:02Z",
        agent_type="general-purpose",
        description="explore race",
    )
    watcher._emit_cycle()
    upgraded = _latest_agent_tool_call(collected, parent_assistant_uuid, tool_use_id)
    assert upgraded is not None
    assert upgraded["subagent_metadata"]["session_id"] == "agent-racesubid"


def test_subagent_discovered_after_history_file_disappears(tmp_path: Path) -> None:
    """A rotated/replaced agent can lose its claude_session_id_history while its main session
    stays watched (already in _session_states). Subagent discovery must still run for known
    sessions, so a subagent that appears AFTER the history file is gone is linked -- not
    stranded on the pending state. (Subagent discovery used to sit behind the history reader's
    early return, so a missing history file silently disabled all further linkage.)"""
    parent_assistant_uuid = "assistant-uuid-rot"
    tool_use_id = "toolu_rot"
    parent_event = _make_agent_tool_use_assistant(
        uuid=parent_assistant_uuid,
        timestamp="2026-01-01T00:00:01Z",
        tool_use_id=tool_use_id,
        description="explore rot",
    )

    agent_state_dir, claude_config_dir, session_id = _setup_agent(tmp_path, [parent_event])
    parent_session_file = claude_config_dir / "projects" / "hash123" / f"{session_id}.jsonl"

    collected: list[tuple[str, list[dict[str, Any]]]] = []
    watcher = ClaudeSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: collected.append((aid, evts)),
    )

    # The main session is discovered and primed while the history file still exists.
    watcher._prime()

    # The agent is rotated/replaced: its history file disappears, but the main session file
    # stays on disk and watched.
    (agent_state_dir / "claude_session_id_history").unlink()

    # A subagent appears only now, after the history file is gone.
    _write_subagent_session(
        parent_session_file,
        agent_id="rotsubid",
        tool_use_id=tool_use_id,
        first_timestamp="2026-01-01T00:00:02Z",
        agent_type="general-purpose",
        description="explore rot",
    )

    # Discovery must still pick it up despite the missing history file, and the card links.
    watcher._emit_cycle()
    upgraded = _latest_agent_tool_call(collected, parent_assistant_uuid, tool_use_id)
    assert upgraded is not None
    assert upgraded["subagent_metadata"]["session_id"] == "agent-rotsubid"


# --- Queue replay scoped to the latest main session ---


def _queue_enqueue_record(
    content: str, session_id: str, timestamp: str = "2026-01-01T00:00:05.000Z"
) -> dict[str, Any]:
    return {
        "type": "queue-operation",
        "operation": "enqueue",
        "timestamp": timestamp,
        "sessionId": session_id,
        "content": content,
    }


def _touch_process_started_marker(agent_state_dir: Path, iso_timestamp: str) -> None:
    """Pin the claude_process_started marker's mtime to the given UTC instant.

    The marker mtime is the process-epoch boundary the queue feed scopes enqueue
    replays by: enqueues stamped before it belong to a dead process.
    """
    marker = agent_state_dir / "claude_process_started"
    marker.touch()
    epoch = datetime.fromisoformat(iso_timestamp).timestamp()
    os.utime(marker, (epoch, epoch))


def _queue_dequeue_record(session_id: str) -> dict[str, Any]:
    return {"type": "queue-operation", "operation": "dequeue", "timestamp": "t", "sessionId": session_id}


def _queued_contents(watcher: ClaudeSessionWatcher) -> list[str]:
    return [entry["content"] for entry in watcher.get_queued_messages()]


def test_dead_session_dangling_enqueues_do_not_replay_alongside_a_newer_session(tmp_path: Path) -> None:
    """A fresh replay over a dead session's dangling enqueues plus a newer main
    session snapshots empty.

    The stopped-agent / restarted-claude case: the old session's parked enqueues
    (no matching leaves) sit in its ledger forever, but the live process's queue
    is exactly the LATEST main session's queue signals, so the priming replay
    must not resurrect them.
    """
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    claude_config_dir = tmp_path / "claude_config"
    projects_dir = claude_config_dir / "projects"
    _write_session_file(
        projects_dir,
        "session-1",
        [_user_event(0), _queue_enqueue_record("stranded in the dead process", "session-1")],
    )
    _write_session_file(projects_dir, "session-2", [_user_event(1)])
    (agent_state_dir / "claude_session_id_history").write_text("session-1\nsession-2\n")

    watcher = _make_watcher(agent_state_dir, claude_config_dir, [])
    watcher._prime()

    assert watcher.get_queued_messages() == []


def test_latest_session_parked_enqueues_replay_on_a_fresh_start(tmp_path: Path) -> None:
    """The backend-restart-mid-turn case: the latest main session's parked
    enqueues (a queue cannot span sessions) are rebuilt by the priming replay,
    netting enqueues against leaves; older sessions contribute nothing."""
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    claude_config_dir = tmp_path / "claude_config"
    projects_dir = claude_config_dir / "projects"
    # The old session's queue history fully netted out while it was alive.
    _write_session_file(
        projects_dir,
        "session-1",
        [_queue_enqueue_record("long committed", "session-1"), _queue_dequeue_record("session-1")],
    )
    # The live session has one committed and one still-parked message.
    _write_session_file(
        projects_dir,
        "session-2",
        [
            _queue_enqueue_record("committed", "session-2"),
            _queue_dequeue_record("session-2"),
            _queue_enqueue_record("parked mid-turn", "session-2"),
        ],
    )
    (agent_state_dir / "claude_session_id_history").write_text("session-1\nsession-2\n")

    watcher = _make_watcher(agent_state_dir, claude_config_dir, [])
    watcher._prime()

    assert _queued_contents(watcher) == ["parked mid-turn"]


def test_new_latest_session_registered_mid_watch_purges_residue(tmp_path: Path) -> None:
    """A new main session registered mid-watch (claude restarted outside minds)
    purges residue on the next discovery cycle, without waiting for the new
    session to emit a queue signal, and the poll broadcasts the empty snapshot."""
    agent_state_dir, claude_config_dir, session_file = _setup_empty_agent(tmp_path)
    watcher = _make_watcher(agent_state_dir, claude_config_dir, [])
    snapshots: list[list[dict[str, Any]]] = []
    watcher.set_queue_snapshot_callback(snapshots.append)

    with open(session_file, "ab") as f:
        f.write((json.dumps(_queue_enqueue_record("live for now", "test-session")) + "\n").encode("utf-8"))
    watcher._emit_cycle()
    assert _queued_contents(watcher) == ["live for now"]
    assert [entry["content"] for entry in snapshots[-1]] == ["live for now"]

    # The restart rotates into a new session file; its ledger emits no queue
    # signal, yet registering it as the new latest must purge the residue.
    _write_session_file(claude_config_dir / "projects", "session-next", [_user_event(1)])
    with open(agent_state_dir / "claude_session_id_history", "a") as f:
        f.write("session-next\n")

    watcher.get_all_events()
    assert watcher.get_queued_messages() == []
    watcher._emit_cycle()
    assert snapshots[-1] == []


def test_truncated_latest_session_re_derives_queue_from_scratch(tmp_path: Path) -> None:
    """An atomic save-rewrite of the latest session file re-derives the queue
    from the rewritten contents instead of double-feeding the same enqueues."""
    agent_state_dir, claude_config_dir, session_file = _setup_empty_agent(tmp_path)
    watcher = _make_watcher(agent_state_dir, claude_config_dir, [])
    watcher.get_all_events()

    with open(session_file, "ab") as f:
        f.write((json.dumps(_queue_enqueue_record("first", "test-session")) + "\n").encode("utf-8"))
        f.write((json.dumps(_queue_enqueue_record("second", "test-session")) + "\n").encode("utf-8"))
    watcher._emit_cycle()
    assert _queued_contents(watcher) == ["first", "second"]

    # Rewritten shorter: only the first enqueue survives. Without the truncation
    # reset the replay would append a duplicate onto the stale entries.
    session_file.write_bytes((json.dumps(_queue_enqueue_record("first", "test-session")) + "\n").encode("utf-8"))
    watcher._emit_cycle()
    assert _queued_contents(watcher) == ["first"]


def test_queued_to_delivered_emits_chip_removal_before_the_transcript_turn(tmp_path: Path) -> None:
    """A3b: when a queued message commits within one poll -- its LEAVE record and its committed
    ``user`` record land together -- the queue snapshot (the chip REMOVAL) is broadcast BEFORE
    the transcript turn. So the message is never shown as both a chip and a turn at once (the
    two-unordered-channels double-show); the departing queue chip leaves before the turn arrives.
    """
    agent_state_dir, claude_config_dir, session_file = _setup_empty_agent(tmp_path)
    order_log: list[str] = []
    watcher = ClaudeSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda _aid, evts: order_log.append(f"turn:{len(evts)}"),
    )
    watcher.set_queue_snapshot_callback(
        lambda snapshot: order_log.append(f"queue:{[entry['content'] for entry in snapshot]}")
    )

    # The message is queued first: a chip appears (a pure-enqueue cycle emits no transcript turn).
    with open(session_file, "ab") as f:
        f.write((json.dumps(_queue_enqueue_record("do the thing", "test-session")) + "\n").encode("utf-8"))
    watcher._emit_cycle()
    assert _queued_contents(watcher) == ["do the thing"]
    assert order_log == ["queue:['do the thing']"]

    order_log.clear()
    # In ONE poll the queued message commits: its LEAVE (dequeue) record and its committed
    # ``user`` turn are appended together, exactly as a Queued->Delivered transition writes them.
    with open(session_file, "ab") as f:
        f.write((json.dumps(_queue_dequeue_record("test-session")) + "\n").encode("utf-8"))
        f.write((json.dumps(_user_event(1, "do the thing")) + "\n").encode("utf-8"))
    watcher._emit_cycle()

    assert watcher.get_queued_messages() == []
    # The chip-removal (empty snapshot) is emitted BEFORE the transcript turn -- never the turn
    # while the chip is still shown.
    assert order_log == ["queue:[]", "turn:1"]


def test_reprime_after_backend_restart_excludes_dead_epoch_enqueues(tmp_path: Path) -> None:
    """claude --resume RE-APPENDS to the same session file, so a backend restart's
    priming replay walks a ledger that can still hold enqueues a killed claude
    process never resolved. Those dead-epoch enqueues must not re-derive as
    queued (they would render as ghost chips the idle backstop later silently
    evaporates); an enqueue stamped after the claude_process_started marker (the
    live process's epoch) still re-derives."""
    agent_state_dir, claude_config_dir, session_file = _setup_empty_agent(tmp_path)
    with open(session_file, "ab") as f:
        f.write(
            (
                json.dumps(
                    _queue_enqueue_record(
                        "died with the old process", "test-session", timestamp="2026-01-01T00:00:05.000Z"
                    )
                )
                + "\n"
            ).encode("utf-8")
        )
        f.write(
            (
                json.dumps(
                    _queue_enqueue_record(
                        "parked in the live process", "test-session", timestamp="2026-01-01T00:01:00.000Z"
                    )
                )
                + "\n"
            ).encode("utf-8")
        )
    # The claude process (re)started between the two enqueues.
    _touch_process_started_marker(agent_state_dir, "2026-01-01T00:00:30Z")

    # A fresh watcher primes over the whole backlog, as a restarted backend does.
    watcher = _make_watcher(agent_state_dir, claude_config_dir, [])
    watcher._prime()

    assert _queued_contents(watcher) == ["parked in the live process"]


def test_truncation_reset_excludes_dead_epoch_enqueues(tmp_path: Path) -> None:
    """The truncation reset re-reads the latest session's ledger from the start;
    like the priming replay, that full replay must not re-derive enqueues from
    before the current process epoch."""
    agent_state_dir, claude_config_dir, session_file = _setup_empty_agent(tmp_path)
    _touch_process_started_marker(agent_state_dir, "2026-01-01T00:00:30Z")
    watcher = _make_watcher(agent_state_dir, claude_config_dir, [])
    watcher.get_all_events()

    live_content = "a long live-epoch message that outsizes the rewritten file"
    with open(session_file, "ab") as f:
        f.write(
            (
                json.dumps(_queue_enqueue_record(live_content, "test-session", timestamp="2026-01-01T00:01:00.000Z"))
                + "\n"
            ).encode("utf-8")
        )
    watcher._emit_cycle()
    assert _queued_contents(watcher) == [live_content]

    # An atomic save-rewrite shrinks the file to a single dead-epoch enqueue: the
    # reset replay must exclude it rather than resurrect a ghost.
    session_file.write_bytes(
        (
            json.dumps(_queue_enqueue_record("ghost", "test-session", timestamp="2026-01-01T00:00:05.000Z")) + "\n"
        ).encode("utf-8")
    )
    watcher._emit_cycle()
    assert _queued_contents(watcher) == []


# --- Main-session discovery: no read-path stalls, history-ordered registration ---


def test_discovery_miss_does_not_stall_the_read_path(tmp_path: Path) -> None:
    """A session listed in history whose file is not on disk yet (an agent's
    startup window) must not make the synchronous read-path discovery wait for
    it; the miss is simply retried on the next cycle. The old inline retry slept
    0.5s per missing session per read, stalling every /events request."""
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    claude_config_dir = tmp_path / "claude_config"
    (claude_config_dir / "projects").mkdir(parents=True)
    (agent_state_dir / "claude_session_id_history").write_text("missing-1\nmissing-2\nmissing-3\n")
    watcher = _make_watcher(agent_state_dir, claude_config_dir, [])

    started_at = time.monotonic()
    assert watcher.get_all_events() == []
    elapsed = time.monotonic() - started_at

    # Three misses used to cost three 0.5s sleeps on this single read; the walk
    # over this tiny tree costs milliseconds, so the bound is generous.
    assert elapsed < 0.5

    # The missing sessions stay unregistered so a later discovery pass retries.
    assert watcher._main_session_ids == []


def test_late_found_session_is_inserted_in_history_order(tmp_path: Path) -> None:
    """A session whose file appears on disk only after a newer session was
    registered must slot into its history position, not the end: an append would
    misorder the merged timeline and misdirect the latest-session gates --
    feeding a dead session's ledger to the queue tracker and resetting the live
    queue derived from the real latest session."""
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    claude_config_dir = tmp_path / "claude_config"
    projects_dir = claude_config_dir / "projects"
    (agent_state_dir / "claude_session_id_history").write_text("session-1\nsession-2\n")
    # Only the newer session's file is on disk at first discovery.
    _write_session_file(projects_dir, "session-2", [_user_event(5)])

    watcher = _make_watcher(agent_state_dir, claude_config_dir, [])
    watcher.get_all_events()
    assert watcher._main_session_ids == ["session-2"]

    # The latest session's ledger feeds the queue tracker while session-1's file
    # is still missing.
    session_2_file = projects_dir / "hash123" / "session-2.jsonl"
    with open(session_2_file, "ab") as f:
        f.write((json.dumps(_queue_enqueue_record("parked in the live session", "session-2")) + "\n").encode("utf-8"))
    watcher._emit_cycle()
    assert _queued_contents(watcher) == ["parked in the live session"]

    # The older session's file lands late; it must register at its history position.
    _write_session_file(projects_dir, "session-1", [_user_event(0)])
    watcher.get_all_events()

    assert watcher._main_session_ids == ["session-1", "session-2"]
    latest = watcher.get_latest_main_session_file()
    assert latest is not None
    assert latest.name == "session-2.jsonl"
    # The merged timeline reads in history order, and the live queue survived:
    # the tracker resets only for a NEW latest session, never a late-found older one.
    assert [e["event_id"] for e in watcher.get_events_at_offset(0, 10)] == ["uuid-0-user", "uuid-5-user"]
    assert _queued_contents(watcher) == ["parked in the live session"]


def test_is_main_session_event_excludes_subagent_sessions(tmp_path: Path) -> None:
    """The predicate that keeps subagent-session events out of the main stream."""
    agent_state_dir, claude_config_dir, session_id = _setup_agent(tmp_path, [])
    watcher = ClaudeSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: None,
    )
    watcher.get_all_events()

    assert watcher.is_main_session_event({"session_id": session_id})
    assert not watcher.is_main_session_event({"session_id": "agent-some-subagent"})
    # Events without a session_id (e.g. plugin-injected app events) stay on the main stream.
    assert watcher.is_main_session_event({"type": "agents_updated"})


def test_watcher_handles_missing_session_file(tmp_path: Path) -> None:
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    claude_config_dir = tmp_path / "claude_config"
    claude_config_dir.mkdir()

    # Write history with a session ID whose file doesn't exist
    (agent_state_dir / "claude_session_id_history").write_text("nonexistent-session\n")

    watcher = ClaudeSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: None,
    )

    result = watcher.get_all_events()
    assert len(result) == 0


# --- Bounded tail/backfill/offset paging over the resident store ---


def _ts(index: int) -> str:
    """A lexicographically sortable, monotonically increasing timestamp."""
    return f"2026-01-01T00:00:00.{index:09d}Z"


def _assistant_line(index: int) -> dict[str, Any]:
    """An assistant message -> exactly one event from one JSONL line."""
    return {
        "type": "assistant",
        "uuid": f"a{index:07d}",
        "timestamp": _ts(index),
        "message": {
            "role": "assistant",
            "model": "claude-test",
            "content": [{"type": "text", "text": f"response {index}"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    }


def _user_multi_line(index: int) -> dict[str, Any]:
    """A user line carrying both text and a tool_result -> TWO events from one line.

    Exercises the multi-event-line path: both the user_message and the
    tool_result share the same source byte range / locator offset.
    """
    return {
        "type": "user",
        "uuid": f"u{index:07d}",
        "timestamp": _ts(index),
        "message": {
            "role": "user",
            "content": [
                {"type": "text", "text": f"message {index}"},
                {"type": "tool_result", "tool_use_id": f"call-{index}", "content": f"output {index}"},
            ],
        },
    }


def _build_two_file_agent(tmp_path: Path, file1_lines: int, file2_lines: int) -> tuple[Path, Path]:
    """Write a resumed conversation across two session files; return (agent_state, claude_config).

    Lines alternate assistant (1 event) and user-multi (2 events) so the
    transcript contains both single- and multi-event lines. Timestamps are
    globally monotonic, with file2 strictly after file1 (a resumed session).
    """
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    claude_config_dir = tmp_path / "claude_config"
    projects_dir = claude_config_dir / "projects"

    counter = 0

    def make_lines(count: int) -> list[dict[str, Any]]:
        nonlocal counter
        lines: list[dict[str, Any]] = []
        for _ in range(count):
            lines.append(_assistant_line(counter) if counter % 2 == 0 else _user_multi_line(counter))
            counter += 1
        return lines

    _write_session_file(projects_dir, "session-1", make_lines(file1_lines))
    _write_session_file(projects_dir, "session-2", make_lines(file2_lines))
    (agent_state_dir / "claude_session_id_history").write_text("session-1\nsession-2\n")
    return agent_state_dir, claude_config_dir


def _make_oracle_watcher(agent_state_dir: Path, claude_config_dir: Path) -> ClaudeSessionWatcher:
    return ClaudeSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda _aid, _evts: None,
    )


def _ids(events: list[dict[str, Any]]) -> list[str]:
    return [e["event_id"] for e in events]


def test_tail_and_backfill_match_oracle_across_files(tmp_path: Path) -> None:
    """get_tail_events / get_backfill_events equal the full-transcript oracle slices."""
    agent_state_dir, claude_config_dir = _build_two_file_agent(tmp_path, file1_lines=120, file2_lines=80)
    watcher = _make_oracle_watcher(agent_state_dir, claude_config_dir)

    oracle = watcher.get_all_events()
    oracle_ids = _ids(oracle)
    # Multi-event lines produce more events than lines.
    assert len(oracle) > 200

    # Tail matches the end of the oracle.
    assert _ids(watcher.get_tail_events(50)) == oracle_ids[-50:]
    assert _ids(watcher.get_tail_events(1)) == oracle_ids[-1:]

    # Backfill before several cursors -- including one that straddles the
    # file-1 / file-2 boundary -- matches the oracle window.
    for cursor_idx in (5, 50, 130, len(oracle) - 1):
        before_id = oracle_ids[cursor_idx]
        expected = oracle_ids[max(0, cursor_idx - 30) : cursor_idx]
        assert _ids(watcher.get_backfill_events(before_id, limit=30)) == expected

    # Backfill before the very first event yields nothing.
    assert watcher.get_backfill_events(oracle_ids[0], limit=30) == []


def test_get_event_offset_reflects_position(tmp_path: Path) -> None:
    """get_event_offset is the global index of an event across resumed files; the
    endpoint returns it so the client can place the loaded window and derive
    whether more history exists above (offset > 0) and below (offset + len < total)."""
    agent_state_dir, claude_config_dir = _build_two_file_agent(tmp_path, file1_lines=40, file2_lines=40)
    watcher = _make_oracle_watcher(agent_state_dir, claude_config_dir)
    oracle_ids = _ids(watcher.get_all_events())

    assert watcher.get_event_offset(oracle_ids[0]) == 0
    assert watcher.get_event_offset(oracle_ids[1]) == 1
    # An event in the second file is indexed past the whole first file.
    assert watcher.get_event_offset(oracle_ids[-1]) == len(oracle_ids) - 1
    assert watcher.get_event_offset("does-not-exist") == -1


def test_offset_and_forward_fetch_match_oracle(tmp_path: Path) -> None:
    """get_events_at_offset (jump) and get_forward_events (page newer) equal the
    oracle slices, including across the file-1/file-2 boundary."""
    agent_state_dir, claude_config_dir = _build_two_file_agent(tmp_path, file1_lines=120, file2_lines=80)
    watcher = _make_oracle_watcher(agent_state_dir, claude_config_dir)
    oracle_ids = _ids(watcher.get_all_events())

    # Jump to an arbitrary offset that straddles the file boundary.
    for offset in (0, 5, 115, len(oracle_ids) - 3):
        expected = oracle_ids[offset : offset + 30]
        assert _ids(watcher.get_events_at_offset(offset, 30)) == expected
    # Offset past the end yields nothing.
    assert watcher.get_events_at_offset(len(oracle_ids), 30) == []

    # Forward paging after a cursor, including across the boundary and at the end.
    for cursor_idx in (0, 100, 130, len(oracle_ids) - 1):
        before_id = oracle_ids[cursor_idx]
        expected = oracle_ids[cursor_idx + 1 : cursor_idx + 1 + 30]
        assert _ids(watcher.get_forward_events(before_id, limit=30)) == expected


def test_get_total_event_count_spans_all_files_and_is_window_independent(tmp_path: Path) -> None:
    """The total count covers the whole transcript (across resumed files) and does
    not change with which tail/backfill window has been read -- the client relies
    on it to size the scrollbar for the full conversation, not the loaded slice."""
    agent_state_dir, claude_config_dir = _build_two_file_agent(tmp_path, file1_lines=120, file2_lines=80)
    watcher = _make_oracle_watcher(agent_state_dir, claude_config_dir)
    total = len(watcher.get_all_events())

    assert watcher.get_total_event_count() == total
    # Reading a bounded tail (far smaller than total, and below the body-cache
    # capacity) must not change the reported total.
    watcher.get_tail_events(5)
    assert watcher.get_total_event_count() == total


def test_backfill_deep_in_history_returns_correct_bodies(tmp_path: Path) -> None:
    """A backfill page deep in history returns the same events a fresh full parse does."""
    agent_state_dir, claude_config_dir = _build_two_file_agent(tmp_path, file1_lines=120, file2_lines=80)
    oracle = _make_oracle_watcher(agent_state_dir, claude_config_dir)
    oracle_events = oracle.get_all_events()
    oracle_ids = _ids(oracle_events)
    body_by_id = {e["event_id"]: e for e in oracle_events}

    watcher = _make_oracle_watcher(agent_state_dir, claude_config_dir)
    watcher.get_tail_events(16)

    # Backfill a window deep in history near the start.
    page = watcher.get_backfill_events(oracle_ids[60], limit=20)
    assert _ids(page) == oracle_ids[40:60]
    # The bodies match the oracle, not just the ids. Separate ifs (not an
    # if/elif chain) keep the comparison exhaustive per type.
    for event in page:
        oracle_event = body_by_id[event["event_id"]]
        assert event["type"] == oracle_event["type"]
        if event["type"] == "tool_result":
            assert event["output_chars"] == oracle_event["output_chars"]
        if event["type"] == "assistant_message":
            assert event["text"] == oracle_event["text"]


def test_paging_backward_recovers_the_entire_transcript_in_order(tmp_path: Path) -> None:
    """Backfill paging from the tail walks the whole transcript without gaps or overlaps."""
    agent_state_dir, claude_config_dir = _build_two_file_agent(tmp_path, file1_lines=150, file2_lines=150)
    watcher = _make_oracle_watcher(agent_state_dir, claude_config_dir)

    all_ids = _ids(_make_oracle_watcher(agent_state_dir, claude_config_dir).get_all_events())

    page_size = 10
    seen = _ids(watcher.get_tail_events(page_size))
    page = watcher.get_backfill_events(seen[0], limit=page_size)
    while page:
        seen = _ids(page) + seen
        page = watcher.get_backfill_events(seen[0], limit=page_size)

    assert seen == all_ids


def test_get_latest_main_session_file_returns_the_only_session(tmp_path: Path) -> None:
    """With a single main session, the accessor resolves that session's JSONL file."""
    agent_state_dir, claude_config_dir, session_id = _setup_agent(tmp_path, [_user_event(1)])
    watcher = _make_watcher(agent_state_dir, claude_config_dir, [])

    resolved = watcher.get_latest_main_session_file()

    assert resolved is not None
    assert resolved.name == f"{session_id}.jsonl"
    assert resolved.exists()


def test_get_latest_main_session_file_prefers_the_newest_session(tmp_path: Path) -> None:
    """When the agent has resumed into a newer session, the accessor returns the latest one."""
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    claude_config_dir = tmp_path / "claude_config"
    projects_dir = claude_config_dir / "projects"
    _write_session_file(projects_dir, "old-session", [_user_event(1)])
    _write_session_file(projects_dir, "new-session", [_user_event(2)])
    # History order is chronological; the newest main session is last.
    (agent_state_dir / "claude_session_id_history").write_text("old-session\nnew-session\n")
    watcher = _make_watcher(agent_state_dir, claude_config_dir, [])

    resolved = watcher.get_latest_main_session_file()

    assert resolved is not None
    assert resolved.name == "new-session.jsonl"


def test_get_latest_main_session_file_none_without_history(tmp_path: Path) -> None:
    """No main session known -> None (there is no live process to tap)."""
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    claude_config_dir = tmp_path / "claude_config"
    (claude_config_dir / "projects").mkdir(parents=True)
    watcher = _make_watcher(agent_state_dir, claude_config_dir, [])

    assert watcher.get_latest_main_session_file() is None


# --- On-demand payload detail ---


def _make_bash_result_line(uuid: str, timestamp: str, call_id: str, output: str) -> str:
    return json.dumps(
        {
            "type": "user",
            "uuid": uuid,
            "timestamp": timestamp,
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": call_id, "content": output}],
            },
        }
    )


def test_get_event_detail_serves_the_full_payloads_from_disk(tmp_path: Path) -> None:
    """Resident events are payload-free; the detail read reconstructs the whole input and
    output from the recorded source byte ranges."""
    big_input = {"command": "echo " + "x" * 5000}
    events = [
        {
            "type": "assistant",
            "uuid": "uuid-a",
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {
                "role": "assistant",
                "model": "claude-test",
                "content": [{"type": "tool_use", "id": "toolu_big", "name": "Bash", "input": big_input}],
            },
        },
    ]
    agent_state_dir, claude_config_dir, session_id = _setup_agent(tmp_path, events)
    session_file = claude_config_dir / "projects" / "hash123" / f"{session_id}.jsonl"
    with open(session_file, "a") as f:
        f.write(_make_bash_result_line("uuid-r", "2026-01-01T00:00:02Z", "toolu_big", "y" * 9000) + "\n")

    watcher = _make_watcher(agent_state_dir, claude_config_dir, [])
    parsed = watcher.get_all_events()
    assistant = next(e for e in parsed if e["type"] == "assistant_message")
    result = next(e for e in parsed if e["type"] == "tool_result")
    assert "input_preview" not in assistant["tool_calls"][0]
    assert "output" not in result

    detail = watcher.get_event_detail(assistant["event_id"])
    assert detail is not None
    assert "x" * 5000 in detail["inputs_by_tool_call_id"]["toolu_big"]
    # Claude's thinking is encrypted and useless; never surfaced.
    assert detail["thinking"] is None

    detail = watcher.get_event_detail(result["event_id"])
    assert detail is not None
    assert detail["output"] == "y" * 9000


def test_get_event_detail_falls_back_to_a_scan_when_the_range_is_stale(tmp_path: Path) -> None:
    """A rewrite that shifts byte offsets under the recorded range still resolves: the
    watcher scans the session file for the event's own identity before giving up."""
    result_line = _make_bash_result_line("uuid-r", "2026-01-01T00:00:02Z", "toolu_1", "the real output")
    agent_state_dir, claude_config_dir, session_id = _setup_agent(tmp_path, [_user_event(0)])
    session_file = claude_config_dir / "projects" / "hash123" / f"{session_id}.jsonl"
    with open(session_file, "a") as f:
        f.write(result_line + "\n")

    watcher = _make_watcher(agent_state_dir, claude_config_dir, [])
    result = next(e for e in watcher.get_all_events() if e["type"] == "tool_result")

    # Shift the file contents under the recorded range WITHOUT shrinking it (a shrink
    # would trigger the truncation reset and re-derive fresh ranges): pad ahead of the
    # recorded offset so the recorded range now reads garbage.
    padding = json.dumps({"type": "noise", "uuid": "zz", "timestamp": "t", "message": None})
    session_file.write_text(padding + "\n" + session_file.read_text())

    detail = watcher.get_event_detail(result["event_id"])
    assert detail is not None
    assert detail["output"] == "the real output"


def test_get_event_detail_answers_none_when_the_source_is_gone(tmp_path: Path) -> None:
    agent_state_dir, claude_config_dir, session_id = _setup_agent(tmp_path, [_user_event(0)])
    session_file = claude_config_dir / "projects" / "hash123" / f"{session_id}.jsonl"
    with open(session_file, "a") as f:
        f.write(_make_bash_result_line("uuid-r", "2026-01-01T00:00:02Z", "toolu_1", "soon gone") + "\n")
    watcher = _make_watcher(agent_state_dir, claude_config_dir, [])
    result = next(e for e in watcher.get_all_events() if e["type"] == "tool_result")

    # The file is rewritten without the result line (same length class does not matter:
    # the range no longer parses AND the scan finds nothing).
    session_file.write_text(json.dumps(_user_event(7)) + "\n")
    assert watcher.get_event_detail(result["event_id"]) is None
    assert watcher.get_event_detail("unknown-event") is None
