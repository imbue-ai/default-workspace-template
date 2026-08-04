"""Tests for the codex session watcher's read paths.

The regression these exist for: the read methods used to serve only what the background
thread had already parsed, so the first request after a system-interface restart could
answer "no transcript" for a rollout sitting on disk -- and the client caches that answer
and never refetches, leaving an empty chat until a page reload. Claude's watcher cannot
lose that race because its read paths re-read from disk on every call; these assert codex
now does the same, without the thread ever running.
"""

import json
from pathlib import Path
from typing import Any

from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.codex.watcher import CodexSessionWatcher
from imbue.system_interface.harnesses.harness_type import HarnessType

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


def _build_watcher(agent_state_dir: Path) -> tuple[CodexSessionWatcher, list[dict[str, Any]]]:
    """A watcher over ``agent_state_dir``, never started, plus the events it broadcasts."""
    broadcast: list[dict[str, Any]] = []
    agent_info = AgentInfo(
        id="agent-test",
        name="test",
        state="RUNNING",
        agent_state_dir=agent_state_dir,
        claude_config_dir=agent_state_dir / "unused",
        harness=HarnessType.CODEX,
    )
    watcher = CodexSessionWatcher.build(agent_info, lambda _agent_id, events: broadcast.extend(events))
    return watcher, broadcast


def test_reads_serve_history_without_the_thread_having_run(tmp_path: Path) -> None:
    """The bug: a read before the loop's first pass answered empty.

    ``start()`` is deliberately never called, which is the whole point -- this is the
    state the very first request after a restart sees.
    """
    _write_rollout(tmp_path, [_user_line("first", "2026-08-03T00:00:01Z"), _user_line("second", "2026-08-03T00:00:02Z")])
    watcher, _ = _build_watcher(tmp_path)

    assert watcher.get_total_event_count() == 2
    assert [event["content"] for event in watcher.get_tail_events(50)] == ["first", "second"]
    assert len(watcher.get_all_events()) == 2


def test_a_read_does_not_stop_the_loop_broadcasting_those_events(tmp_path: Path) -> None:
    """Reads advance the read cursor; the emit bookmark is separate.

    Without the split, a read would consume the bytes and the loop would then have
    nothing to broadcast -- so the live stream would silently lose exactly the events a
    reader happened to pull in first.
    """
    _write_rollout(tmp_path, [_user_line("only", "2026-08-03T00:00:01Z")])
    watcher, broadcast = _build_watcher(tmp_path)

    assert len(watcher.get_tail_events(50)) == 1
    assert broadcast == []

    watcher._emit_unsent()
    assert [event["content"] for event in broadcast] == ["only"]

    # Idempotent: a second pass re-broadcasts nothing.
    watcher._emit_unsent()
    assert len(broadcast) == 1


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
