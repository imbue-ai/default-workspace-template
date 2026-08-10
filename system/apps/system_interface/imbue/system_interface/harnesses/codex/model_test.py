"""Unit tests for the Codex model resolver's switch side, a live-state conformance check, and
the stop/flush control-line writers.

The live READ is harness-neutral (the shared reader), so the resolver only owns switching.
The conformance test pins the reader against the codex-in-minds patch's NEW output schema --
CI cannot execute the Rust binary, so the fixture is hand-written to that schema. The reader's
graceful handling of the OLD schema (``reasoning_effort``/``service_tier``) lives in
``harnesses/model_test.py``.

The stop-executor tests drive :func:`execute_codex_stop_to_composer` with plain fakes (no
mocks): a scripted watcher whose rollout view flips after the mirror clear stands in for the
patched binary's abort, and injected ``mark_idle`` / ``now`` / ``sleep`` observe the marker
settle without real time. The endpoint-level dispatch (restart fallback, HTTP mapping) lives
in ``server_test.py``.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr_codex.codex_config import get_codex_home
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.codex.model import CODEX_CATALOG
from imbue.system_interface.harnesses.codex.model import CODEX_STATE_RELATIVE_PATH
from imbue.system_interface.harnesses.codex.model import CodexFlushTapStatus
from imbue.system_interface.harnesses.codex.model import CodexModelResolver
from imbue.system_interface.harnesses.codex.model import codex_control_path
from imbue.system_interface.harnesses.codex.model import execute_codex_stop_to_composer
from imbue.system_interface.harnesses.codex.model import flush_codex_queue_atomic
from imbue.system_interface.harnesses.harness_type import HarnessType
from imbue.system_interface.harnesses.model import ModelAxis
from imbue.system_interface.harnesses.model import ModelIdentity
from imbue.system_interface.harnesses.model import SwitchMode
from imbue.system_interface.harnesses.model import match_option
from imbue.system_interface.harnesses.model import model_state_path
from imbue.system_interface.harnesses.model import read_model_identity


def _agent_info(tmp_path: Path) -> AgentInfo:
    return AgentInfo(
        id="agent-1",
        name="a",
        state="RUNNING",
        agent_state_dir=tmp_path,
        claude_config_dir=tmp_path / "unused",
        harness=HarnessType.CODEX,
    )


def test_catalog_is_eager_then_reconcile() -> None:
    assert CODEX_CATALOG.switch_mode == SwitchMode.EAGER_THEN_RECONCILE


def test_state_relative_path_is_under_codex_home() -> None:
    # The registered relative dir must resolve to the same place get_codex_home does, so the
    # shared reader finds the file the patched codex writes under CODEX_HOME.
    assert model_state_path(Path("/agent"), CODEX_STATE_RELATIVE_PATH) == get_codex_home(Path("/agent")) / (
        "minds_model_state.json"
    )


def test_reader_matches_the_new_patch_schema(tmp_path: Path) -> None:
    # A hand-written fixture of the codex-in-minds patch's NEW {model, effort, fast} output.
    state_path = model_state_path(tmp_path, CODEX_STATE_RELATIVE_PATH)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"model": "gpt-5.6-sol", "effort": "high", "fast": True}))
    identity = read_model_identity(state_path)
    assert identity is not None
    assert identity == ModelIdentity(model_id="gpt-5.6-sol", effort="high", fast=True)
    matched = match_option(identity, CODEX_CATALOG.options)
    assert matched is not None
    assert matched.id == "gpt-5.6-sol"


def test_switch_model_and_effort_send_one_model_command(tmp_path: Path) -> None:
    # Codex applies model + effort together, so a model change sends one
    # `/model <model> <effort>` -- not a separate /effort.
    resolver = CodexModelResolver.build(_agent_info(tmp_path))
    sent: list[str] = []
    result = resolver.switch(
        ModelIdentity(model_id="gpt-5.6-terra", effort="high", fast=False),
        frozenset({ModelAxis.MODEL, ModelAxis.EFFORT}),
        lambda line: sent.append(line) or True,
    )
    assert result.ok
    assert sent == ["/model gpt-5.6-terra high"]


def test_switch_effort_only_still_goes_through_model(tmp_path: Path) -> None:
    # Effort has no standalone codex command; it rides /model with the current model.
    resolver = CodexModelResolver.build(_agent_info(tmp_path))
    sent: list[str] = []
    result = resolver.switch(
        ModelIdentity(model_id="gpt-5.6-sol", effort="xhigh", fast=False),
        frozenset({ModelAxis.EFFORT}),
        lambda line: sent.append(line) or True,
    )
    assert result.ok
    assert sent == ["/model gpt-5.6-sol xhigh"]


def test_switch_fast_toggle_sends_only_fast(tmp_path: Path) -> None:
    resolver = CodexModelResolver.build(_agent_info(tmp_path))
    sent: list[str] = []
    result = resolver.switch(
        ModelIdentity(model_id="gpt-5.6-sol", effort="medium", fast=True),
        frozenset({ModelAxis.FAST}),
        lambda line: sent.append(line) or True,
    )
    assert result.ok
    assert sent == ["/fast on"]


def test_switch_with_no_axes_sends_nothing(tmp_path: Path) -> None:
    resolver = CodexModelResolver.build(_agent_info(tmp_path))
    sent: list[str] = []
    result = resolver.switch(
        ModelIdentity(model_id="gpt-5.6-sol", effort="medium", fast=False),
        frozenset(),
        lambda line: sent.append(line) or True,
    )
    assert result.ok
    assert sent == []


# =============================================================================
# Stop executor (retract + marker settle) and the locked flush writer
# =============================================================================


class _FakeStopWatcher:
    """Scripts the watcher slice the stop/flush paths read: the rollout view before the
    retract, the view after the mirror clear (the patched binary's abort landing), and the
    queued block. ``clear_calls`` counts ``clear_queue`` invocations."""

    def __init__(
        self,
        block: str,
        events: list[dict[str, Any]],
        events_after_clear: list[dict[str, Any]] | None = None,
    ) -> None:
        self._block = block
        self._events = events
        self._events_after_clear = events_after_clear
        self.clear_calls = 0

    def get_all_events(self, session_id: str | None = None) -> list[dict[str, Any]]:
        if self.clear_calls > 0 and self._events_after_clear is not None:
            return list(self._events_after_clear)
        return list(self._events)

    def get_queued_block(self) -> str:
        return self._block

    def clear_queue(self) -> None:
        self.clear_calls += 1


def _open_turn_events(turn_id: str) -> list[dict[str, Any]]:
    return [{"type": "special", "kind": "turn_started", "turn_id": turn_id}]


def _aborted_turn_events(turn_id: str) -> list[dict[str, Any]]:
    return [
        {"type": "special", "kind": "turn_started", "turn_id": turn_id},
        {"type": "special", "kind": "turn_aborted", "turn_id": turn_id},
    ]


def _unexpected_restart() -> str:
    pytest.fail("the native retract path must not fall back to the restart hammer")


def _unexpected_mark_idle() -> None:
    pytest.fail("mark_idle must not run when no retract was written")


def _unexpected_sleep(_seconds: float) -> None:
    pytest.fail("the settle watch must not wait when its verdict is already visible")


def test_stop_marks_idle_once_the_retract_abort_is_observed(tmp_path: Path) -> None:
    """The core marker-hygiene fix: after the retract line lands and the rollout shows the
    turn aborted, the executor clears the stranded lifecycle markers (via the injected
    mngr_codex primitive) -- and still hands the captured block back."""
    watcher = _FakeStopWatcher(
        "bring me back to edit", _open_turn_events("tid-1"), events_after_clear=_aborted_turn_events("tid-1")
    )
    idle_calls: list[bool] = []

    block = execute_codex_stop_to_composer(
        agent_state_dir=tmp_path,
        watcher=watcher,
        mark_idle=lambda: idle_calls.append(True),
        restart_drain_to_base=_unexpected_restart,
        sleep=_unexpected_sleep,
    )

    assert block == "bring me back to edit"
    assert watcher.clear_calls == 1
    assert idle_calls == [True]
    assert codex_control_path(tmp_path).read_text().splitlines() == ['{"retract_turn_id": "tid-1"}']


def test_stop_leaves_markers_alone_when_the_abort_is_never_observed(tmp_path: Path) -> None:
    """Confirm-before-clear: if the retracted turn never shows a boundary within the settle
    deadline (version skew: an old binary skipped the line), the markers are NOT cleared --
    the turn may genuinely still be running. The handback itself is unaffected."""
    watcher = _FakeStopWatcher("bring me back to edit", _open_turn_events("tid-1"))
    idle_calls: list[bool] = []
    ticks = {"n": 0.0}

    def _fake_now() -> float:
        return ticks["n"]

    def _fake_sleep(_seconds: float) -> None:
        ticks["n"] += 1.0

    block = execute_codex_stop_to_composer(
        agent_state_dir=tmp_path,
        watcher=watcher,
        mark_idle=lambda: idle_calls.append(True),
        restart_drain_to_base=_unexpected_restart,
        now=_fake_now,
        sleep=_fake_sleep,
        settle_deadline_seconds=3.0,
        poll_interval_seconds=0.2,
    )

    assert block == "bring me back to edit"
    assert idle_calls == []
    assert codex_control_path(tmp_path).read_text().splitlines() == ['{"retract_turn_id": "tid-1"}']


def test_stop_skips_idle_marking_when_a_new_turn_already_opened(tmp_path: Path) -> None:
    """If a fresh turn opened in the gap between the abort and the settle poll, the new
    turn's own lifecycle hooks legitimately own the markers -- clearing them would flip a
    genuinely RUNNING agent to WAITING."""
    after = _aborted_turn_events("tid-1") + _open_turn_events("tid-2")
    watcher = _FakeStopWatcher("queued text", _open_turn_events("tid-1"), events_after_clear=after)
    idle_calls: list[bool] = []

    block = execute_codex_stop_to_composer(
        agent_state_dir=tmp_path,
        watcher=watcher,
        mark_idle=lambda: idle_calls.append(True),
        restart_drain_to_base=_unexpected_restart,
        sleep=_unexpected_sleep,
    )

    assert block == "queued text"
    assert idle_calls == []


def test_stop_still_hands_back_the_block_when_mark_idle_fails(tmp_path: Path) -> None:
    """Marker cleanup is best-effort: the interrupt already succeeded, so a failing idle
    primitive must not turn a completed stop into an error or lose the handback."""

    def _raise() -> None:
        raise OSError("marker state unreachable")

    watcher = _FakeStopWatcher(
        "bring me back to edit", _open_turn_events("tid-1"), events_after_clear=_aborted_turn_events("tid-1")
    )

    block = execute_codex_stop_to_composer(
        agent_state_dir=tmp_path,
        watcher=watcher,
        mark_idle=_raise,
        restart_drain_to_base=_unexpected_restart,
        sleep=_unexpected_sleep,
    )

    assert block == "bring me back to edit"


def test_stop_with_no_open_turn_hands_back_the_queued_block_and_clears_the_mirror(tmp_path: Path) -> None:
    """Message conservation on the idle-stop path: queued messages with NO turn running have
    nothing to drain into, so the stop returns them to the composer and clears the mirror
    (no ghost chips) -- writing no control line, since there is nothing to retract."""
    completed = [
        {"type": "special", "kind": "turn_started", "turn_id": "tid-1"},
        {"type": "special", "kind": "turn_completed", "turn_id": "tid-1"},
    ]
    watcher = _FakeStopWatcher("still queued", completed)
    idle_calls: list[bool] = []

    block = execute_codex_stop_to_composer(
        agent_state_dir=tmp_path,
        watcher=watcher,
        mark_idle=lambda: idle_calls.append(True),
        restart_drain_to_base=_unexpected_restart,
        sleep=_unexpected_sleep,
    )

    assert block == "still queued"
    assert watcher.clear_calls == 1
    assert not codex_control_path(tmp_path).exists()
    # No retract was written, so there are no stranded markers to settle.
    assert idle_calls == []


def test_stop_with_no_open_turn_and_empty_mirror_is_a_pure_noop(tmp_path: Path) -> None:
    watcher = _FakeStopWatcher("", [])

    block = execute_codex_stop_to_composer(
        agent_state_dir=tmp_path,
        watcher=watcher,
        mark_idle=_unexpected_mark_idle,
        restart_drain_to_base=_unexpected_restart,
        sleep=_unexpected_sleep,
    )

    assert block == ""
    assert watcher.clear_calls == 0
    assert not codex_control_path(tmp_path).exists()


def test_flush_writes_the_control_line_for_an_open_turn(tmp_path: Path) -> None:
    watcher = _FakeStopWatcher("", _open_turn_events("tid-7"))
    status = flush_codex_queue_atomic(tmp_path, watcher)
    assert status == CodexFlushTapStatus.TAPPED
    assert codex_control_path(tmp_path).read_text().splitlines() == ['{"target_turn_id": "tid-7"}']


def test_flush_with_no_open_turn_writes_nothing(tmp_path: Path) -> None:
    watcher = _FakeStopWatcher("", [])
    status = flush_codex_queue_atomic(tmp_path, watcher)
    assert status == CodexFlushTapStatus.NO_OPEN_TURN
    assert not codex_control_path(tmp_path).exists()
