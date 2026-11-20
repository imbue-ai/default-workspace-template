"""Unit tests for grok's send: it holds the message for the flush worker and never types."""

import os
from pathlib import Path
from typing import NoReturn

import pytest

from imbue.chat.activity_state import ACTIVE_MARKER_FILENAME
from imbue.chat.harnesses.grok.model import GROK_CATALOG
from imbue.chat.harnesses.grok.queue_tracker import MAX_DELIVERY_ATTEMPTS
from imbue.chat.harnesses.grok.queue_tracker import get_tracker
from imbue.chat.harnesses.grok.session import GrokHarnessSession
from imbue.chat.harnesses.grok.testing import make_grok_agent_state_dir
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.session import SendOutcome
from imbue.chat.harnesses.session import SessionDeps
from imbue.mngr_grok.grok_config import PROCESS_STARTED_MARKER_FILENAME


def _unused(*args: object, **kwargs: object) -> NoReturn:
    raise AssertionError("a grok send never types: the flush worker is the only typist")


def _session(
    state_dir: Path, *, lifecycle_state: str | None = "WAITING", is_starting_up: bool = False
) -> GrokHarnessSession:
    session = GrokHarnessSession.build(
        SessionDeps(
            harness=HarnessType.GROK,
            state_dir=state_dir,
            model_state_path=state_dir / "model_state.json",
            send_to_harness=_unused,
            notify_agents_changed=lambda: None,
            is_harness_starting_up=lambda: is_starting_up,
            is_tracked=lambda: True,
            read_lifecycle_state=lambda: lifecycle_state,
            on_queue_snapshot=lambda snapshot: None,
            on_user_turn=lambda event: None,
            recompute_activity=lambda: None,
            clear_queue_state=lambda: None,
            catalog_options=lambda: GROK_CATALOG.options,
            build_interrupter=_unused,
            build_shoulder_tap=lambda agent_info: None,
        )
    )
    assert isinstance(session, GrokHarnessSession)
    return session


def _held(state_dir: Path) -> list[tuple[str, bool]]:
    return [(str(entry["content"]), bool(entry["is_sending"])) for entry in get_tracker(state_dir).snapshot()]


def test_a_send_to_an_idle_grok_is_held_as_sending_for_the_worker(tmp_path: Path) -> None:
    state_dir = make_grok_agent_state_dir(tmp_path)

    assert _session(state_dir).send("hello", "m1") is SendOutcome.OK

    assert _held(state_dir) == [("hello", True)]


@pytest.mark.parametrize(
    ("lifecycle_state", "is_marker_present"),
    [
        pytest.param("RUNNING", False, id="mngr_reports_running"),
        pytest.param("WAITING", True, id="the_active_marker_is_ahead_of_the_reported_state"),
        pytest.param(None, False, id="the_agent_is_not_tracked_yet"),
    ],
)
def test_a_send_during_a_turn_is_held_as_queued(
    tmp_path: Path, lifecycle_state: str | None, is_marker_present: bool
) -> None:
    state_dir = make_grok_agent_state_dir(tmp_path)
    if is_marker_present:
        (state_dir / ACTIVE_MARKER_FILENAME).touch()

    assert _session(state_dir, lifecycle_state=lifecycle_state).send("later", "m1") is SendOutcome.OK

    assert _held(state_dir) == [("later", False)]


def test_a_send_while_grok_runs_background_work_is_held_as_sending(tmp_path: Path) -> None:
    """RUNNING_BACKGROUND has no turn in flight, so the worker is about to type the message."""
    state_dir = make_grok_agent_state_dir(tmp_path)

    assert _session(state_dir, lifecycle_state="RUNNING_BACKGROUND").send("next", "m1") is SendOutcome.OK

    assert _held(state_dir) == [("next", True)]


@pytest.mark.parametrize(
    ("lifecycle_state", "is_starting_up"),
    [
        pytest.param("WAITING", True, id="grok_is_starting"),
        pytest.param("STOPPED", False, id="the_agent_is_stopped"),
        pytest.param("DONE", False, id="the_grok_process_exited"),
    ],
)
def test_a_send_is_not_ready_while_grok_starts_or_mngr_reports_it_dead(
    tmp_path: Path, lifecycle_state: str, is_starting_up: bool
) -> None:
    state_dir = make_grok_agent_state_dir(tmp_path)

    outcome = _session(state_dir, lifecycle_state=lifecycle_state, is_starting_up=is_starting_up).send("hi", "m1")

    assert outcome is SendOutcome.NOT_READY
    assert _held(state_dir) == []


def test_a_send_after_grok_relaunched_drops_the_old_sessions_messages_and_keeps_its_own(tmp_path: Path) -> None:
    state_dir = make_grok_agent_state_dir(tmp_path)
    marker = state_dir / PROCESS_STARTED_MARKER_FILENAME
    marker.touch()
    os.utime(marker, ns=(1_000_000_000, 1_000_000_000))
    session = _session(state_dir)
    session.send("for the old grok", "m1")
    os.utime(marker, ns=(2_000_000_000, 2_000_000_000))

    session.send("for the new grok", "m2")

    assert [content for content, _is_sending in _held(state_dir)] == ["for the new grok"]


def test_the_tap_is_offered_only_while_a_held_message_has_attempts_left_and_nothing_is_sending(tmp_path: Path) -> None:
    state_dir = make_grok_agent_state_dir(tmp_path)
    session = _session(state_dir, lifecycle_state="RUNNING")
    session.send("held", "m1")
    assert session.is_tap_available(has_queued=True) is True

    queue = get_tracker(state_dir)
    _block, claimed, generation = queue.begin_flush()
    assert session.is_sending() is True
    assert session.is_tap_available(has_queued=True) is False
    queue.finish_flush(claimed, generation, delivered=())
    for _attempt in range(MAX_DELIVERY_ATTEMPTS - 1):
        _block, claimed, generation = queue.begin_flush()
        queue.finish_flush(claimed, generation, delivered=())

    assert session.is_tap_available(has_queued=True) is False
