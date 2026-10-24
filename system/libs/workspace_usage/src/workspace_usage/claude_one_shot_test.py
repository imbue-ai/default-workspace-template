import json
from pathlib import Path

from workspace_usage.claude_one_shot import claude_session_args, record_claude_one_shot
from workspace_usage.turn import usage_turn

_SESSION = "4d2f8e61-9a07-4c3b-b5e8-1f6d0a7c2e93"


def _write_session(config_dir: Path, request_ids: list[str]) -> Path:
    log = config_dir / "projects" / "-tmp-one-shot" / f"{_SESSION}.jsonl"
    log.parent.mkdir(parents=True)
    lines = []
    for index, request_id in enumerate(request_ids):
        lines.append(
            {
                "type": "user",
                "sessionId": _SESSION,
                "promptId": f"prompt-{index}",
                "timestamp": f"2026-10-22T12:00:0{index * 2}Z",
            }
        )
        lines.append(
            {
                "type": "assistant",
                "requestId": request_id,
                "sessionId": _SESSION,
                "timestamp": f"2026-10-22T12:00:0{index * 2 + 1}Z",
                "message": {
                    "model": "claude-haiku-4-5-20251001",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            }
        )
    log.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return log


def test_session_args_name_the_session() -> None:
    assert claude_session_args("0b4c") == ["--session-id", "0b4c"]


def test_a_one_shots_calls_are_read_from_its_log_and_recorded_then_the_log_is_deleted(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "account"
    log = _write_session(config_dir, ["req_1", "req_2"])
    (config_dir / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"billingType": "stripe_subscription"}})
    )
    environ = {"IMBUE_USAGE_DATA_DIR": str(tmp_path / "usage"), "MNGR_AGENT_ID": "ws-9"}

    recorded = record_claude_one_shot(
        config_dir,
        _SESSION,
        purpose="CHAT_TITLE",
        context={"origin_kind": "CHAT", "origin_id": "c-1"},
        environ=environ,
    )

    assert recorded == 2
    assert not log.exists()
    events = [
        json.loads(line)
        for line in (tmp_path / "usage" / "events" / "chat-c-1.jsonl")
        .read_text()
        .splitlines()
    ]
    assert [event["event_id"] for event in events] == ["req_1", "req_2"]
    assert events[0]["billing_mode"] == "SUBSCRIPTION"
    assert events[0]["attribution"] == {
        "workspace_id": "ws-9",
        "origin_kind": "CHAT",
        "origin_id": "c-1",
        "harness": "claude",
        "purpose": "CHAT_TITLE",
        "session_id": _SESSION,
        "turn_id": "prompt-0",
        "call_source": "MAIN",
    }


def test_a_kept_session_stays_and_the_process_context_is_used_by_default(
    tmp_path: Path,
) -> None:
    log = _write_session(tmp_path / "account", ["req_3"])
    environ = {
        "IMBUE_USAGE_DATA_DIR": str(tmp_path / "usage"),
        "IMBUE_USAGE_CONTEXT": "origin_kind=ROUTINE,origin_id=nightly",
    }

    assert (
        record_claude_one_shot(
            tmp_path / "account", _SESSION, delete_log=False, environ=environ
        )
        == 1
    )
    assert log.exists()
    assert (tmp_path / "usage" / "events" / "routine-nightly.jsonl").is_file()


def test_a_session_without_a_log_records_nothing(tmp_path: Path) -> None:
    assert (
        record_claude_one_shot(
            tmp_path, _SESSION, environ={"IMBUE_USAGE_DATA_DIR": str(tmp_path)}
        )
        == 0
    )


def test_a_one_shot_run_inside_a_usage_turn_records_its_calls_under_that_turn(
    tmp_path: Path,
) -> None:
    _write_session(tmp_path / "account", ["req_4", "req_5"])
    environ = {
        "IMBUE_USAGE_DATA_DIR": str(tmp_path / "usage"),
        "IMBUE_USAGE_CONTEXT": "origin_kind=APP,origin_id=triage",
    }

    with usage_turn("email-42"):
        record_claude_one_shot(tmp_path / "account", _SESSION, environ=environ)

    events_file = tmp_path / "usage" / "events" / "app-triage.jsonl"
    events = [json.loads(line) for line in events_file.read_text().splitlines()]
    assert [event["attribution"]["turn_id"] for event in events] == [
        "email-42",
        "email-42",
    ]
