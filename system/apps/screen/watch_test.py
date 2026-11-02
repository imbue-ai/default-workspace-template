import json
from pathlib import Path

from mock_supervisor_test import FakeSupervisor
from screen.data_types import ScreenStopped
from screen.events import EVENTS_RELATIVE_PATH
from screen.watch import decide_group_end
from screen.watch import end_group


def _payload(program: str, group: str = "screen-stack", expected: str = "0") -> str:
    return f"processname:{program} groupname:{group} from_state:RUNNING expected:{expected} pid:123"


def test_a_member_exit_ends_the_rest_of_the_group() -> None:
    decision = decide_group_end("PROCESS_STATE_EXITED", _payload("screen-neko"))
    assert decision.stopped is not None
    assert decision.stopped.member == "screen-neko"
    assert decision.stopped.reason == "screen-neko exited unexpectedly"
    assert decision.members_to_stop == ("screen-session", "screen-pulse", "screen-xorg")


def test_an_expected_exit_and_a_fatal_start_are_named_as_such() -> None:
    expected = decide_group_end("PROCESS_STATE_EXITED", _payload("screen-session", expected="1"))
    assert expected.stopped is not None
    assert expected.stopped.reason == "screen-session exited"
    fatal = decide_group_end("PROCESS_STATE_FATAL", _payload("screen-xorg"))
    assert fatal.stopped is not None
    assert fatal.stopped.reason == "screen-xorg could not be started"


def test_other_events_and_other_programs_are_ignored() -> None:
    assert decide_group_end("PROCESS_STATE_STOPPED", _payload("screen-neko")).stopped is None
    assert decide_group_end("PROCESS_STATE_EXITED", _payload("browser", group="browser")).stopped is None
    assert decide_group_end("PROCESS_STATE_EXITED", _payload("screen", group="screen")).stopped is None
    assert decide_group_end("PROCESS_STATE_EXITED", "garbage").members_to_stop == ()


def test_end_group_stops_the_others_records_the_event_and_notifies(tmp_path: Path) -> None:
    supervisor = FakeSupervisor()
    notified: list[ScreenStopped] = []
    decision = decide_group_end("PROCESS_STATE_EXITED", _payload("screen-xorg"))
    end_group(decision, supervisor, tmp_path, notified.append)
    assert supervisor.calls == [("stop", "screen-neko"), ("stop", "screen-session"), ("stop", "screen-pulse")]
    assert notified == [ScreenStopped(member="screen-xorg", reason="screen-xorg exited unexpectedly")]
    lines = (tmp_path / EVENTS_RELATIVE_PATH).read_text().splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["type"] == "screen_stopped"
    assert event["source"] == "screen"
    assert event["detail"] == {"member": "screen-xorg", "reason": "screen-xorg exited unexpectedly"}
    assert event["event_id"].startswith("scr-")
    assert event["timestamp"].endswith("+00:00")


def test_end_group_does_nothing_for_an_event_that_is_not_ours(tmp_path: Path) -> None:
    supervisor = FakeSupervisor()
    end_group(decide_group_end("PROCESS_STATE_STOPPED", _payload("screen-neko")), supervisor, tmp_path, lambda _: None)
    assert supervisor.calls == []
    assert not (tmp_path / EVENTS_RELATIVE_PATH).exists()
