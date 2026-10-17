from screen.cli import EXIT_BUSY
from screen.cli import EXIT_ERROR
from screen.cli import EXIT_OK
from screen.cli import EXIT_PREEMPTED
from screen.cli import EXIT_TIMEOUT
from screen.cli import describe_controller
from screen.cli import parse_answer_body
from screen.cli import render_acquire_answer


def test_acquire_answers_map_to_the_fleet_exit_codes() -> None:
    assert render_acquire_answer({"ok": True}, "me")[1] == EXIT_OK
    assert render_acquire_answer({"ok": False, "status": "busy_human", "enqueued": True}, "me")[1] == EXIT_PREEMPTED
    assert render_acquire_answer({"ok": False, "status": "busy_human", "enqueued": False}, "me")[1] == EXIT_BUSY
    assert (
        render_acquire_answer(
            {"ok": False, "status": "busy_agent", "enqueued": True, "controller": "agent", "agent_name": "Bob"}, "me"
        )[1]
        == EXIT_BUSY
    )
    assert render_acquire_answer({"ok": False, "status": "starting"}, "me")[1] == EXIT_BUSY
    assert render_acquire_answer({"ok": False, "status": "timed_out"}, "me")[1] == EXIT_TIMEOUT
    assert render_acquire_answer({"ok": False, "status": "weird", "detail": "boom"}, "me") == ("boom", EXIT_ERROR)


def test_busy_agent_line_names_the_holder() -> None:
    line, _ = render_acquire_answer(
        {"ok": False, "status": "busy_agent", "controller": "agent", "agent_name": "Bob", "agent_id": "b"}, "me"
    )
    assert "held by agent Bob" in line


def test_describe_controller() -> None:
    assert describe_controller({"controller": "agent", "agent_id": "me"}, "me") == "you"
    assert describe_controller({"controller": "agent", "agent_id": "b", "agent_name": "Bob"}, "me") == "agent Bob"
    assert describe_controller({"controller": "human", "human_name": "Josh"}, "me") == "the human (Josh)"
    assert describe_controller({"controller": "nobody"}, "me") == "nobody"


def test_answer_bodies_are_parsed_only_when_json() -> None:
    assert parse_answer_body(b'{"pid": 4}', "application/json") == {"pid": 4}
    assert parse_answer_body(b"\x89PNG\r\n", "image/png") == {}
    assert parse_answer_body(b"not json", "application/json; charset=utf-8") == {}
    assert parse_answer_body(b"[1, 2]", "application/json") == {}
    assert parse_answer_body(b"", "application/json") == {}
