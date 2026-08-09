"""Unit tests for the claude shoulder-tap executor.

Covers the pure verdict lattice over synthetic raw tails + mirror states (including the
pre-baseline-leaves race and its idle-gap variant), the raw-tail reader, every gate, and
the orchestration (chord delivery, recovery send, and the status mapping). The live chord
itself is verified manually via tmux, not here (repo convention).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from imbue.system_interface.harnesses.claude.session_parser import INTERRUPT_SENTINEL_TEXT
from imbue.system_interface.harnesses.claude.tap import ClaudeTapStatus
from imbue.system_interface.harnesses.claude.tap import TapVerdict
from imbue.system_interface.harnesses.claude.tap import compute_tail_facts
from imbue.system_interface.harnesses.claude.tap import deadline_verdict
from imbue.system_interface.harnesses.claude.tap import execute_claude_shoulder_tap
from imbue.system_interface.harnesses.claude.tap import poll_verdict
from imbue.system_interface.harnesses.claude.tap import read_raw_tail

# --- raw-record builders -------------------------------------------------------------


def _user_line(text: str) -> str:
    return json.dumps({"type": "user", "message": {"role": "user", "content": text}})


def _assistant_line(text: str = "sure") -> str:
    return json.dumps({"type": "assistant", "message": {"role": "assistant", "content": text}})


_SENTINEL_LINE = _user_line(INTERRUPT_SENTINEL_TEXT)
_MID_TOOL_SENTINEL_LINE = _user_line("[Request interrupted by user for tool use]")


# --- compute_tail_facts --------------------------------------------------------------


def test_tail_facts_empty_tail() -> None:
    facts = compute_tail_facts([])
    assert facts.has_interrupt_sentinel is False
    assert facts.has_assistant_answer is False


def test_tail_facts_sentinel_only_is_dangling() -> None:
    facts = compute_tail_facts([_SENTINEL_LINE])
    assert facts.has_interrupt_sentinel is True
    assert facts.has_assistant_answer is False


def test_tail_facts_sentinel_then_assistant_is_answered() -> None:
    facts = compute_tail_facts([_SENTINEL_LINE, _assistant_line()])
    assert facts.has_interrupt_sentinel is True
    assert facts.has_assistant_answer is True


def test_tail_facts_assistant_before_sentinel_stays_dangling() -> None:
    """An assistant record BEFORE the last sentinel does not count as an answer to it."""
    facts = compute_tail_facts([_assistant_line(), _SENTINEL_LINE])
    assert facts.has_interrupt_sentinel is True
    assert facts.has_assistant_answer is False


def test_tail_facts_no_sentinel_with_assistant() -> None:
    facts = compute_tail_facts([_user_line("hi"), _assistant_line()])
    assert facts.has_interrupt_sentinel is False
    assert facts.has_assistant_answer is True


def test_tail_facts_mid_tool_variant_is_not_the_tap_sentinel() -> None:
    """The mid-tool ``for tool use`` variant is the sibling interrupt plan's; not matched here."""
    facts = compute_tail_facts([_MID_TOOL_SENTINEL_LINE])
    assert facts.has_interrupt_sentinel is False


def test_tail_facts_ignores_non_json_lines() -> None:
    facts = compute_tail_facts(["not json at all", _SENTINEL_LINE])
    assert facts.has_interrupt_sentinel is True


# --- poll_verdict / deadline_verdict (the lattice) -----------------------------------


def _facts(*, sentinel: bool, answer: bool) -> Any:
    return compute_tail_facts(
        ([_SENTINEL_LINE] if sentinel else [])
        + ([_assistant_line()] if answer else [])
    )


def test_poll_verdict_early_flushed_on_drained_with_answer() -> None:
    assert poll_verdict(mirror_is_empty=True, facts=_facts(sentinel=True, answer=True)) == TapVerdict.FLUSHED


def test_poll_verdict_waits_when_drained_but_no_answer() -> None:
    """A drained mirror with a dangling sentinel keeps watching (never an early FLUSHED)."""
    assert poll_verdict(mirror_is_empty=True, facts=_facts(sentinel=True, answer=False)) is None


def test_poll_verdict_waits_when_mirror_not_empty() -> None:
    assert poll_verdict(mirror_is_empty=False, facts=_facts(sentinel=True, answer=True)) is None


def test_deadline_verdict_not_flushed_when_mirror_nonempty() -> None:
    assert deadline_verdict(mirror_is_empty=False, facts=_facts(sentinel=False, answer=False)) == TapVerdict.NOT_FLUSHED


def test_deadline_verdict_needs_recovery_on_pre_baseline_leaves_race() -> None:
    """Drained mirror + a dangling sentinel = the chord cancelled the flushed follow-on turn."""
    assert (
        deadline_verdict(mirror_is_empty=True, facts=_facts(sentinel=True, answer=False))
        == TapVerdict.NEEDS_RECOVERY
    )


def test_deadline_verdict_flushed_on_idle_gap_variant() -> None:
    """Drained mirror with no sentinel (natural flush already happened) = FLUSHED, not failure."""
    assert deadline_verdict(mirror_is_empty=True, facts=_facts(sentinel=False, answer=False)) == TapVerdict.FLUSHED


def test_deadline_verdict_flushed_when_sentinel_answered() -> None:
    assert deadline_verdict(mirror_is_empty=True, facts=_facts(sentinel=True, answer=True)) == TapVerdict.FLUSHED


# --- read_raw_tail -------------------------------------------------------------------


def test_read_raw_tail_returns_only_lines_after_baseline(tmp_path: Path) -> None:
    session = tmp_path / "s.jsonl"
    session.write_text("before-baseline\n")
    baseline = session.stat().st_size
    with session.open("a") as f:
        f.write("after-one\nafter-two\n")
    assert read_raw_tail(session, baseline) == ["after-one", "after-two"]


def test_read_raw_tail_drops_trailing_partial_line(tmp_path: Path) -> None:
    session = tmp_path / "s.jsonl"
    session.write_text("base\n")
    baseline = session.stat().st_size
    with session.open("a") as f:
        f.write("complete\npartial-no-newline")
    assert read_raw_tail(session, baseline) == ["complete"]


def test_read_raw_tail_empty_when_not_grown(tmp_path: Path) -> None:
    session = tmp_path / "s.jsonl"
    session.write_text("base\n")
    assert read_raw_tail(session, session.stat().st_size) == []


# --- orchestration: gates + verdict routing ------------------------------------------


class _FakeTapWatcher:
    """A watcher stand-in whose mirror snapshots and session growth a test scripts."""

    def __init__(
        self,
        queue_snapshots: list[list[dict[str, Any]]],
        session_file: Path | None,
        on_refresh: Callable[[int], None] | None = None,
    ) -> None:
        self._queue_snapshots = queue_snapshots
        self._session_file = session_file
        self._on_refresh = on_refresh
        self.events_calls = 0
        self.queue_calls = 0

    def get_all_events(self, session_id: str | None = None) -> list[dict[str, Any]]:
        self.events_calls += 1
        if self._on_refresh is not None:
            self._on_refresh(self.events_calls)
        return []

    def get_queued_messages(self) -> list[dict[str, Any]]:
        index = min(self.queue_calls, len(self._queue_snapshots) - 1)
        self.queue_calls += 1
        return self._queue_snapshots[index]

    def get_latest_main_session_file(self) -> Path | None:
        return self._session_file


_QUEUED = [{"queued_id": "q1", "content": "hi"}]


def _make_agent_paths(
    tmp_path: Path,
    *,
    active: bool = True,
    permissions_waiting: bool = False,
    bind: bool = True,
    binding_predates_marker: bool = True,
) -> tuple[Path, Path]:
    """Create the state dir (markers) and config dir (keybindings), returning both key paths."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    keybindings_path = config_dir / "keybindings.json"
    if bind:
        keybindings_path.write_text(
            json.dumps({"bindings": [{"context": "Chat", "bindings": {"meta+q": "chat:cancel"}}]})
        )
    marker = state_dir / "claude_process_started"
    marker.write_text("")
    if bind:
        # Order the binding and the process marker so the gate reads the intended state.
        keybindings_mtime = 1000 if binding_predates_marker else 3000
        os.utime(keybindings_path, (keybindings_mtime, keybindings_mtime))
        os.utime(marker, (2000, 2000))
    if active:
        (state_dir / "active").write_text("")
    if permissions_waiting:
        (state_dir / "permissions_waiting").write_text("")
    return state_dir, keybindings_path


def _stepping_now(values: list[float]) -> Callable[[], float]:
    """A monotonic clock stand-in that returns successive ``values`` (last repeats)."""
    index = {"i": 0}

    def now() -> float:
        value = values[min(index["i"], len(values) - 1)]
        index["i"] += 1
        return value

    return now


def test_execute_nothing_queued_is_a_noop_and_still_provisions_binding(tmp_path: Path) -> None:
    """An empty mirror short-circuits to nothing_queued; the chord is still provisioned."""
    state_dir, keybindings_path = _make_agent_paths(tmp_path, bind=False)
    assert not keybindings_path.exists()
    presses: list[bool] = []
    result = execute_claude_shoulder_tap(
        agent_state_dir=state_dir,
        keybindings_path=keybindings_path,
        watcher=_FakeTapWatcher([[]], None),
        press_chord=lambda: presses.append(True) or True,
        send_recovery=lambda _text: True,
    )
    assert result.status == ClaudeTapStatus.NOTHING_QUEUED
    assert presses == []
    # ensure_chat_cancel_tap_keybinding ran (self-provision) even on the no-op path.
    assert keybindings_path.exists()


def test_execute_no_open_turn_when_active_marker_absent(tmp_path: Path) -> None:
    state_dir, keybindings_path = _make_agent_paths(tmp_path, active=False)
    presses: list[bool] = []
    result = execute_claude_shoulder_tap(
        agent_state_dir=state_dir,
        keybindings_path=keybindings_path,
        watcher=_FakeTapWatcher([_QUEUED], None),
        press_chord=lambda: presses.append(True) or True,
        send_recovery=lambda _text: True,
    )
    assert result.status == ClaudeTapStatus.NO_OPEN_TURN
    assert presses == []


def test_execute_permissions_waiting_returns_dialog_status(tmp_path: Path) -> None:
    state_dir, keybindings_path = _make_agent_paths(tmp_path, permissions_waiting=True)
    presses: list[bool] = []
    result = execute_claude_shoulder_tap(
        agent_state_dir=state_dir,
        keybindings_path=keybindings_path,
        watcher=_FakeTapWatcher([_QUEUED], None),
        press_chord=lambda: presses.append(True) or True,
        send_recovery=lambda _text: True,
    )
    assert result.status == ClaudeTapStatus.PERMISSIONS_WAITING
    assert presses == []


def test_execute_binding_not_active_when_written_after_launch(tmp_path: Path) -> None:
    state_dir, keybindings_path = _make_agent_paths(tmp_path, binding_predates_marker=False)
    presses: list[bool] = []
    result = execute_claude_shoulder_tap(
        agent_state_dir=state_dir,
        keybindings_path=keybindings_path,
        watcher=_FakeTapWatcher([_QUEUED], None),
        press_chord=lambda: presses.append(True) or True,
        send_recovery=lambda _text: True,
    )
    assert result.status == ClaudeTapStatus.BINDING_NOT_ACTIVE
    assert presses == []


def test_execute_flushed_presses_chord_and_returns_tapped(tmp_path: Path) -> None:
    """All gates pass; the flushed turn produces an answer past the baseline -> tapped."""
    state_dir, keybindings_path = _make_agent_paths(tmp_path)
    session = tmp_path / "session.jsonl"
    session.write_text(_user_line("hello") + "\n")

    def grow_with_answer(events_call: int) -> None:
        # The first watch-loop refresh (events call #2) is when the flushed turn's answer lands.
        if events_call == 2:
            with session.open("a") as f:
                f.write(_assistant_line() + "\n")

    watcher = _FakeTapWatcher([_QUEUED, []], session, on_refresh=grow_with_answer)
    presses: list[bool] = []
    recoveries: list[str] = []
    result = execute_claude_shoulder_tap(
        agent_state_dir=state_dir,
        keybindings_path=keybindings_path,
        watcher=watcher,
        press_chord=lambda: presses.append(True) or True,
        send_recovery=lambda text: recoveries.append(text) or True,
    )
    assert result.status == ClaudeTapStatus.TAPPED
    assert presses == [True]
    # FLUSHED needs no recovery message.
    assert recoveries == []


def test_execute_needs_recovery_sends_notification_and_returns_tapped(tmp_path: Path) -> None:
    """Mirror drains but a dangling sentinel persists to the deadline -> recovery sent, tapped."""
    state_dir, keybindings_path = _make_agent_paths(tmp_path)
    session = tmp_path / "session.jsonl"
    session.write_text(_user_line("hello") + "\n")

    def grow_with_sentinel(events_call: int) -> None:
        if events_call == 2:
            with session.open("a") as f:
                f.write(_SENTINEL_LINE + "\n")

    watcher = _FakeTapWatcher([_QUEUED, []], session, on_refresh=grow_with_sentinel)
    recoveries: list[str] = []
    result = execute_claude_shoulder_tap(
        agent_state_dir=state_dir,
        keybindings_path=keybindings_path,
        watcher=watcher,
        press_chord=lambda: True,
        send_recovery=lambda text: recoveries.append(text) or True,
        now=_stepping_now([0.0, 100.0]),
        sleep=lambda _s: None,
    )
    assert result.status == ClaudeTapStatus.TAPPED
    assert len(recoveries) == 1
    assert recoveries[0].startswith("<task-notification>")


def test_execute_not_flushed_when_mirror_never_drains(tmp_path: Path) -> None:
    state_dir, keybindings_path = _make_agent_paths(tmp_path)
    session = tmp_path / "session.jsonl"
    session.write_text(_user_line("hello") + "\n")
    recoveries: list[str] = []
    result = execute_claude_shoulder_tap(
        agent_state_dir=state_dir,
        keybindings_path=keybindings_path,
        watcher=_FakeTapWatcher([_QUEUED, _QUEUED], session),
        press_chord=lambda: True,
        send_recovery=lambda text: recoveries.append(text) or True,
        now=_stepping_now([0.0, 100.0]),
        sleep=lambda _s: None,
    )
    assert result.status == ClaudeTapStatus.NOT_FLUSHED
    # Nothing was resent on a NOT_FLUSHED outcome.
    assert recoveries == []


def test_execute_chord_send_failure_returns_error(tmp_path: Path) -> None:
    state_dir, keybindings_path = _make_agent_paths(tmp_path)
    session = tmp_path / "session.jsonl"
    session.write_text(_user_line("hello") + "\n")
    result = execute_claude_shoulder_tap(
        agent_state_dir=state_dir,
        keybindings_path=keybindings_path,
        watcher=_FakeTapWatcher([_QUEUED, []], session),
        press_chord=lambda: False,
        send_recovery=lambda _text: True,
    )
    assert result.status == ClaudeTapStatus.CHORD_SEND_FAILED


def test_execute_recovery_send_failure_returns_error(tmp_path: Path) -> None:
    state_dir, keybindings_path = _make_agent_paths(tmp_path)
    session = tmp_path / "session.jsonl"
    session.write_text(_user_line("hello") + "\n")

    def grow_with_sentinel(events_call: int) -> None:
        if events_call == 2:
            with session.open("a") as f:
                f.write(_SENTINEL_LINE + "\n")

    result = execute_claude_shoulder_tap(
        agent_state_dir=state_dir,
        keybindings_path=keybindings_path,
        watcher=_FakeTapWatcher([_QUEUED, []], session, on_refresh=grow_with_sentinel),
        press_chord=lambda: True,
        send_recovery=lambda _text: False,
        now=_stepping_now([0.0, 100.0]),
        sleep=lambda _s: None,
    )
    assert result.status == ClaudeTapStatus.RECOVERY_SEND_FAILED
