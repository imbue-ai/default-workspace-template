"""Tests for the quick-change note hook, driven through the real wrapper so its cheap exits and the
reading half are both exercised.

Each test builds a repo root under ``tmp_path`` with the chat app's quick-change log, runs the hook
the way the harness does (payload on stdin, the agent's environment), and reads what it printed.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

_HOOK = Path(__file__).resolve().parent / "agent_quick_change_notes.sh"

_CHAT = "chat-main"
_OTHER_CHAT = "chat-other"
_QUICK_CHAT = "chat-quick"
_CAREFUL_CHAT = "chat-quick-careful"


def _chat_data_dir(root: Path) -> Path:
    return root / "data" / ".apps" / "chat"


def _append(root: Path, *records: dict[str, Any]) -> None:
    log = _chat_data_dir(root) / "quick_changes.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


def _routed_and_reported(
    root: Path, request_id: str, chat_id: str, status: str = "done"
) -> None:
    _append(
        root,
        {
            "type": "request",
            "request_id": request_id,
            "chat_id": chat_id,
            "requested_at": "2026-11-09T10:00:00+00:00",
            "message": "make the header dark blue",
            "app": "todo",
        },
        {
            "type": "forward",
            "request_id": request_id,
            "status": "forwarded",
            "quick_change_chat_id": _QUICK_CHAT,
            "at": "2026-11-09T10:00:01+00:00",
        },
        {
            "type": "report",
            "request_id": request_id,
            "status": status,
            "report": "Todo: header is now dark blue; window refreshed.",
            "reported_at": "2026-11-09T10:00:30+00:00",
        },
    )


def _run(
    root: Path,
    chat_id: str | None = _CHAT,
    event: str = "PreToolUse",
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {"PATH": os.environ["PATH"], "MNGR_AGENT_WORK_DIR": str(root)}
    if chat_id is not None:
        env["MINDS_CHAT_ID"] = chat_id
    env.update(extra_env or {})
    payload = {"hook_event_name": event, "tool_name": "Bash", "tool_input": {}}
    return subprocess.run(
        ["bash", str(_HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        cwd=root,
        timeout=30,
        check=False,
    )


def _context(completed: subprocess.CompletedProcess[str]) -> str:
    output = json.loads(completed.stdout)["hookSpecificOutput"]
    return output["additionalContext"]


def test_a_new_report_from_this_chat_is_passed_on_as_one_note(tmp_path: Path) -> None:
    _routed_and_reported(tmp_path, "r1", _CHAT)

    completed = _run(tmp_path)

    assert completed.returncode == 0
    lines = _context(completed).splitlines()
    assert len(lines) == 1
    assert "Todo: header is now dark blue" in lines[0]
    assert "keep it when you edit them" in lines[0]


def test_the_note_answers_under_the_event_that_ran_it(tmp_path: Path) -> None:
    _routed_and_reported(tmp_path, "r1", _CHAT)

    completed = _run(tmp_path, event="UserPromptSubmit")

    assert (
        json.loads(completed.stdout)["hookSpecificOutput"]["hookEventName"]
        == "UserPromptSubmit"
    )


def test_a_report_already_passed_on_is_not_passed_on_again(tmp_path: Path) -> None:
    _routed_and_reported(tmp_path, "r1", _CHAT)
    _run(tmp_path)

    completed = _run(tmp_path)

    assert completed.returncode == 0
    assert completed.stdout == ""


def test_a_later_report_is_passed_on_without_the_earlier_one(tmp_path: Path) -> None:
    _routed_and_reported(tmp_path, "r1", _CHAT)
    _run(tmp_path)
    _append(
        tmp_path,
        {"type": "request", "request_id": "r2", "chat_id": _CHAT, "app": "notes"},
        {
            "type": "report",
            "request_id": "r2",
            "status": "done",
            "report": "Notes: font is larger.",
            "reported_at": "2026-11-09T11:00:00+00:00",
        },
    )

    completed = _run(tmp_path)

    lines = _context(completed).splitlines()
    assert len(lines) == 1
    assert "Notes: font is larger." in lines[0]


def test_a_report_routed_from_another_chat_is_not_passed_on(tmp_path: Path) -> None:
    _routed_and_reported(tmp_path, "r1", _OTHER_CHAT)

    completed = _run(tmp_path)

    assert completed.returncode == 0
    assert completed.stdout == ""


def test_the_quick_change_agent_gets_no_note(tmp_path: Path) -> None:
    _routed_and_reported(tmp_path, "r1", _QUICK_CHAT)
    (_chat_data_dir(tmp_path) / "quick_change_agent.json").write_text(
        json.dumps({"chat_id": _QUICK_CHAT, "created_at": "2026-11-09T09:00:00+00:00"}),
        encoding="utf-8",
    )

    completed = _run(tmp_path, chat_id=_QUICK_CHAT)

    assert completed.returncode == 0
    assert completed.stdout == ""


def _write_per_tier_agent_record(root: Path) -> None:
    record = {
        "fast": {"chat_id": _QUICK_CHAT, "created_at": "2026-11-09T09:00:00+00:00"},
        "careful": {
            "chat_id": _CAREFUL_CHAT,
            "created_at": "2026-11-09T09:30:00+00:00",
        },
    }
    _chat_data_dir(root).mkdir(parents=True, exist_ok=True)
    (_chat_data_dir(root) / "quick_change_agent.json").write_text(
        json.dumps(record), encoding="utf-8"
    )


def test_the_careful_quick_change_agent_in_a_per_tier_record_gets_no_note(
    tmp_path: Path,
) -> None:
    _routed_and_reported(tmp_path, "r1", _CAREFUL_CHAT)
    _write_per_tier_agent_record(tmp_path)

    completed = _run(tmp_path, chat_id=_CAREFUL_CHAT)

    assert completed.returncode == 0
    assert completed.stdout == ""


def test_the_fast_quick_change_agent_in_a_per_tier_record_gets_no_note(
    tmp_path: Path,
) -> None:
    _routed_and_reported(tmp_path, "r1", _QUICK_CHAT)
    _write_per_tier_agent_record(tmp_path)

    completed = _run(tmp_path, chat_id=_QUICK_CHAT)

    assert completed.returncode == 0
    assert completed.stdout == ""


def test_a_main_chat_beside_a_per_tier_record_still_gets_its_note(
    tmp_path: Path,
) -> None:
    _routed_and_reported(tmp_path, "r1", _CHAT)
    _write_per_tier_agent_record(tmp_path)

    completed = _run(tmp_path)

    assert "Todo: header is now dark blue" in _context(completed)


def test_a_worker_with_no_chat_gets_no_note(tmp_path: Path) -> None:
    _routed_and_reported(tmp_path, "r1", _CHAT)

    completed = _run(tmp_path, chat_id=None, extra_env={"MNGR_AGENT_ID": _CHAT})

    assert completed.returncode == 0
    assert completed.stdout == ""


def test_a_claude_subagent_gets_no_note(tmp_path: Path) -> None:
    _routed_and_reported(tmp_path, "r1", _CHAT)

    completed = _run(tmp_path, extra_env={"MNGR_CLAUDE_SUBAGENT_PROXY_CHILD": "1"})

    assert completed.returncode == 0
    assert completed.stdout == ""


def test_no_log_means_no_note(tmp_path: Path) -> None:
    completed = _run(tmp_path)

    assert completed.returncode == 0
    assert completed.stdout == ""
