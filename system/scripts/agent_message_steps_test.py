"""Tests for agent_message_steps.py: the prompt hook names the id reserved for the message, and the wait hook
holds a tk start or close of a reserved id until its step exists, or answers at once when it never will."""

from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path

import pytest
from script_modules_testing import load_script_module

hooks = load_script_module("agent_message_steps_for_tests", "agent_message_steps.py")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHAT_ID = "agent-0123456789abcdef0123456789abcdef"
_OTHER_CHAT_ID = "agent-fedcba9876543210fedcba9876543210"


class _Folders:
    def __init__(self, tmp_path: Path) -> None:
        self.tickets_dir = tmp_path / ".tickets"
        self.reservations_dir = tmp_path / "reservations"
        self.tickets_dir.mkdir()
        self.reservations_dir.mkdir()

    @property
    def environ(self) -> dict[str, str]:
        return {
            "TICKETS_DIR": str(self.tickets_dir),
            hooks.RESERVATIONS_DIR_ENV: str(self.reservations_dir),
            "MINDS_CHAT_ID": _CHAT_ID,
        }

    def reserve(self, step_id: str, message: str, chat_id: str = _CHAT_ID) -> None:
        """A reservation as the chat app writes it (MessageStepReservation)."""
        record = {
            "step_id": step_id,
            "chat_id": chat_id,
            "message_id": "message-1",
            "message_text": message,
            "reserved_at": "2026-10-26T21:33:03.000000Z",
        }
        (self.reservations_dir / f"{step_id}.json").write_text(json.dumps(record))

    def settle(
        self, step_id: str, outcome: str, target: tuple[str, str] | None = None
    ) -> None:
        """An outcome as the chat app records it, with the open step a joining message names."""
        settled = self.reservations_dir / hooks.SETTLED_DIRNAME
        settled.mkdir(exist_ok=True)
        record = {"step_id": step_id, "outcome": outcome}
        if target is not None:
            record.update(target_id=target[0], target_title=target[1])
        (settled / f"{step_id}.json").write_text(json.dumps(record))
        (self.reservations_dir / f"{step_id}.json").unlink()


def _run(
    argv: list[str],
    payload: dict[str, object],
    environ: dict[str, str],
    max_wait_seconds: float = 1.0,
):
    stdout = io.StringIO()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("sys.stdout", stdout)
        exit_code = hooks.main(
            argv,
            environ=environ,
            stdin=io.StringIO(json.dumps(payload)),
            max_wait_seconds=max_wait_seconds,
        )
    return exit_code, stdout.getvalue().strip()


@pytest.fixture
def folders(tmp_path: Path) -> _Folders:
    return _Folders(tmp_path)


def test_the_prompt_hook_names_the_id_reserved_for_the_message(
    folders: _Folders,
) -> None:
    folders.reserve("wor-step-ab12", "Add a dark mode to the app.")

    exit_code, printed = _run(
        ["prompt"], {"prompt": "Add a dark mode to the app.\n"}, folders.environ
    )

    assert exit_code == 0
    context = json.loads(printed)["hookSpecificOutput"]["additionalContext"]
    assert (
        "tk start wor-step-ab12" in context and "never create your own step" in context
    )


def test_the_prompt_hook_is_silent_for_a_message_with_no_reservation(
    folders: _Folders,
) -> None:
    folders.reserve("wor-step-ab12", "Add a dark mode to the app.")

    assert _run(["prompt"], {"prompt": "Typed into the terminal"}, folders.environ) == (
        0,
        "",
    )


def test_the_prompt_hook_ignores_another_chats_reservation(folders: _Folders) -> None:
    folders.reserve(
        "wor-step-ab12", "Add a dark mode to the app.", chat_id=_OTHER_CHAT_ID
    )

    assert _run(
        ["prompt"], {"prompt": "Add a dark mode to the app."}, folders.environ
    ) == (0, "")


def test_a_repeated_message_gets_its_newest_reservation() -> None:
    earlier = hooks.PendingStep(
        "wor-step-aaaa", _CHAT_ID, "Add notes", "2026-10-26T21:30:00.000000Z"
    )
    latest = hooks.PendingStep(
        "wor-step-bbbb", _CHAT_ID, "Add notes", "2026-10-26T21:33:00.000000Z"
    )

    assert hooks.step_for_prompt([latest, earlier], "Add notes", _CHAT_ID) == latest


def test_starting_a_step_already_written_goes_straight_through(
    folders: _Folders,
) -> None:
    (folders.tickets_dir / "wor-step-ab12.md").write_text(
        "---\nid: wor-step-ab12\n---\n# Add a dark mode\n"
    )

    payload = {"tool_name": "Bash", "tool_input": {"command": "tk start wor-step-ab12"}}

    assert _run(["wait"], payload, folders.environ) == (0, "")


def test_starting_a_step_judged_not_a_task_is_declined_at_once(
    folders: _Folders,
) -> None:
    folders.reserve("wor-step-ab12", "thanks!")
    folders.settle("wor-step-ab12", hooks.OUTCOME_NOT_A_TASK)
    payload = {"tool_name": "Bash", "tool_input": {"command": "tk start wor-step-ab12"}}

    exit_code, printed = _run(["wait"], payload, folders.environ, max_wait_seconds=30.0)

    assert exit_code == 0
    decision = json.loads(printed)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "judged not to be a task" in decision["permissionDecisionReason"]


def test_starting_a_step_that_could_not_be_created_fails(
    folders: _Folders, capsys: pytest.CaptureFixture[str]
) -> None:
    folders.reserve("wor-step-ab12", "Add a dark mode")
    folders.settle("wor-step-ab12", hooks.OUTCOME_FAILED)
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "tk close wor-step-ab12 'Done'"},
    }

    exit_code, _ = _run(["wait"], payload, folders.environ, max_wait_seconds=30.0)

    assert exit_code == 2
    assert "could not be created" in capsys.readouterr().err


def _denial(printed: str) -> str:
    decision = json.loads(printed)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    return decision["permissionDecisionReason"]


def _write_step(folders: _Folders, step_id: str, title: str, *fields: str) -> None:
    front = "\n".join([f"id: {step_id}", *fields])
    (folders.tickets_dir / f"{step_id}.md").write_text(
        f"---\n{front}\n---\n# {title}\n"
    )


@pytest.mark.parametrize(
    ("outcome", "target_fields", "expected"),
    [
        ("same_as", ["status: in_progress"], "tk start wor-step-qz01"),
        ("same_as", ["status: closed"], "already done"),
        (
            "cancels",
            ["status: closed", "cancelled: 2026-10-27T22:53:48.000000Z"],
            "do not start or close wor-step-qz01",
        ),
        ("cancels", ["status: closed"], "already done before it arrived"),
    ],
)
def test_starting_a_message_that_named_an_open_step_says_what_became_of_it(
    folders: _Folders, outcome: str, target_fields: list[str], expected: str
) -> None:
    _write_step(folders, "wor-step-qz01", "Add a quiz", *target_fields)
    folders.reserve("wor-step-ab12", "about the quiz")
    folders.settle("wor-step-ab12", outcome, target=("wor-step-qz01", "Add a quiz"))
    payload = {"tool_name": "Bash", "tool_input": {"command": "tk start wor-step-ab12"}}

    exit_code, printed = _run(["wait"], payload, folders.environ, max_wait_seconds=30.0)

    assert exit_code == 0
    assert expected in _denial(printed)


def test_starting_a_cancelled_step_is_declined(folders: _Folders) -> None:
    _write_step(
        folders,
        "wor-step-qz01",
        "Add a quiz",
        "status: closed",
        "cancelled: 2026-10-27T22:53:48.000000Z",
    )
    payload = {"tool_name": "Bash", "tool_input": {"command": "tk start wor-step-qz01"}}

    exit_code, printed = _run(["wait"], payload, folders.environ)

    assert exit_code == 0
    assert 'Step wor-step-qz01 "Add a quiz" was called off' in _denial(printed)


def test_closing_a_replaced_step_points_at_its_replacement(folders: _Folders) -> None:
    _write_step(
        folders,
        "wor-step-qz01",
        "Add a quiz",
        "status: closed",
        "replaced-by: wor-step-mc02",
    )
    _write_step(folders, "wor-step-mc02", "Add a multiple-choice quiz", "status: open")
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "tk close wor-step-qz01 'Done'"},
    }

    exit_code, printed = _run(["wait"], payload, folders.environ)

    assert exit_code == 0
    reason = _denial(printed)
    assert 'replaced by step wor-step-mc02 "Add a multiple-choice quiz"' in reason
    assert "tk start wor-step-mc02" in reason


def test_the_wait_ends_when_the_step_is_written(folders: _Folders) -> None:
    folders.reserve("wor-step-ab12", "Add a dark mode")
    step_path = folders.tickets_dir / "wor-step-ab12.md"
    poll_count = 0

    def write_on_third_poll(_seconds: float) -> None:
        nonlocal poll_count
        poll_count += 1
        if poll_count == 3:
            step_path.write_text("---\nid: wor-step-ab12\n---\n# Add a dark mode\n")

    outcome = hooks.wait_for_step(
        "wor-step-ab12",
        folders.tickets_dir,
        folders.reservations_dir,
        30.0,
        sleep=write_on_third_poll,
    )

    assert outcome == hooks.OUTCOME_WRITTEN
    assert poll_count == 3


def test_a_command_with_no_step_id_is_left_alone(folders: _Folders) -> None:
    payload = {"tool_name": "Bash", "tool_input": {"command": "tk steps"}}

    assert _run(["wait"], payload, folders.environ) == (0, "")


def test_the_hooks_run_as_wired_with_a_plain_python3(folders: _Folders) -> None:
    folders.reserve("wor-step-ab12", "Make the sidebar collapsible")

    completed = subprocess.run(
        [
            "python3",
            str(_REPO_ROOT / "system" / "scripts" / "agent_message_steps.py"),
            "prompt",
        ],
        input=json.dumps({"prompt": "Make the sidebar collapsible"}),
        env={**os.environ, **folders.environ},
        capture_output=True,
        text=True,
        check=True,
    )

    assert (
        "wor-step-ab12"
        in json.loads(completed.stdout)["hookSpecificOutput"]["additionalContext"]
    )
