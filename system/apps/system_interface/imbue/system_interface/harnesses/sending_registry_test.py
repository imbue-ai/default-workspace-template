from collections.abc import Callable
from pathlib import Path

import pytest

from imbue.system_interface.harnesses.harness_type import HarnessType
from imbue.system_interface.harnesses.sending_registry import SendingRegistry
from imbue.system_interface.harnesses.session import FileHarnessSession
from imbue.system_interface.harnesses.session import SendOutcome
from imbue.system_interface.harnesses.session import SessionDeps


def test_empty_registry_has_no_block() -> None:
    registry = SendingRegistry.build()
    assert registry.in_flight_texts() == []
    assert registry.concatenated_block() == ""


def test_records_are_returned_in_send_order() -> None:
    registry = SendingRegistry.build()
    registry.record("t1", "first")
    registry.record("t2", "second")
    assert registry.in_flight_texts() == ["first", "second"]
    assert registry.concatenated_block() == "first\nsecond"


def test_resolve_removes_only_the_named_token() -> None:
    registry = SendingRegistry.build()
    registry.record("t1", "first")
    registry.record("t2", "second")
    registry.resolve("t1")
    assert registry.in_flight_texts() == ["second"]


def test_resolve_of_unknown_token_is_a_noop() -> None:
    registry = SendingRegistry.build()
    registry.record("t1", "first")
    registry.resolve("nope")
    assert registry.in_flight_texts() == ["first"]


def test_duplicate_content_is_kept_distinct_by_token() -> None:
    registry = SendingRegistry.build()
    registry.record("t1", "same text")
    registry.record("t2", "same text")
    assert registry.in_flight_texts() == ["same text", "same text"]
    registry.resolve("t1")
    assert registry.in_flight_texts() == ["same text"]


def test_re_recording_a_token_replaces_in_place_without_duplicating() -> None:
    registry = SendingRegistry.build()
    registry.record("t1", "original")
    registry.record("t1", "updated")
    assert registry.in_flight_texts() == ["updated"]


def test_clear_drops_everything() -> None:
    registry = SendingRegistry.build()
    registry.record("t1", "first")
    registry.record("t2", "second")
    registry.clear()
    assert registry.in_flight_texts() == []


def _file_session(send_to_harness: "Callable[[str], bool] | None" = None) -> "FileHarnessSession":
    """A FileHarnessSession over inert deps -- only the Sending surface is exercised."""
    deps = SessionDeps(
        harness=HarnessType.CLAUDE,
        state_dir=Path("/nonexistent"),
        model_state_path=Path("/nonexistent/model_state.json"),
        send_to_harness=send_to_harness if send_to_harness is not None else lambda text: True,
        notify_agents_changed=lambda: None,
        is_tracked=lambda: True,
        on_queue_snapshot=lambda snapshot: None,
        on_user_turn=lambda event: None,
        recompute_activity=lambda: None,
        clear_queue_state=lambda: None,
        catalog_options=lambda: (),
        build_interrupter=lambda agent_info: (_ for _ in ()).throw(AssertionError("unused")),
        build_shoulder_tap=lambda agent_info: None,
    )
    return FileHarnessSession.build(deps)


def test_session_send_tracks_and_resolves_sending_state() -> None:
    """The Sending record exists exactly for the send's in-flight window (contract A1)."""
    observed: list[str] = []
    holder: list[FileHarnessSession] = []

    # Capture the in-flight block DURING the blocking send: the record must be live then.
    def send_and_observe(text: str) -> bool:
        observed.append(holder[0].in_flight_block())
        return True

    session = _file_session(send_and_observe)
    holder.append(session)
    assert session.send("first", "t1") == SendOutcome.OK
    assert observed == ["first"]
    # Resolved after the send: nothing in flight, tap no longer greyed by Sending.
    assert session.in_flight_block() == ""
    assert session.is_sending() is False


def test_session_send_failure_resolves_the_record_too() -> None:
    session = _file_session(lambda text: False)
    assert session.send("hi", "stable-42") == SendOutcome.FAILED
    assert session.in_flight_block() == ""


def test_session_is_tap_available_greys_while_sending() -> None:
    during: list[bool] = []
    holder: list[FileHarnessSession] = []

    def send_and_probe(text: str) -> bool:
        during.append(holder[0].is_tap_available(has_queued=True))
        return True

    session = _file_session(send_and_probe)
    holder.append(session)
    assert session.is_tap_available(has_queued=True) is True
    assert session.is_tap_available(has_queued=False) is False
    session.send("hi", "t1")
    assert during == [False]


def test_session_send_exception_still_resolves_the_record() -> None:
    """An exception from the blocking send must not leak the Sending record: a leak would
    grey the shoulder tap for the session's lifetime and re-inject the same text into
    every later stop's returned block."""

    def send_and_raise(text: str) -> bool:
        raise RuntimeError("mngr messenger blew up")

    session = _file_session(send_and_raise)
    with pytest.raises(RuntimeError):
        session.send("doomed", "t-boom")
    assert session.is_sending() is False
    assert session.in_flight_block() == ""
