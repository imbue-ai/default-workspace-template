"""The update operations a client asks for, run against a staged workspace."""

import json
import os
import stat
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from inline_snapshot import snapshot

from workspace_update_control import control
from workspace_update_control.agents import read_observed_agents
from workspace_update_control.dispatcher import AgentEnvironment
from workspace_update_control.testing import (
    SERVICES_AGENT_ID,
    STAGED_CREATED_CHAT,
    ScriptedToolAnswer,
    StagedUpdateWorkspace,
    full_state_line,
    labels_of,
    listed_agent,
    observed_agent,
    observed_services_agent,
    run_in_place,
    stage_update_workspace,
    write_event_lines,
)

_NOW = 1_792_000_000.0
_FINISHED_AT = _NOW + 7.0
_CEILING = "minds-v0.9.0"
_RUNNING_CHAT = "update-to-minds-v0-8-7"
_DISPATCHED_CHAT = "update-to-minds-v0-9-0"

AgentsReader = Callable[[], Sequence[Mapping[str, str]] | None]


def _arm(
    staged: StagedUpdateWorkspace,
    *,
    ceiling: str = _CEILING,
    override_ref: str = "",
    is_auto_armed: bool = False,
    now: float = _NOW,
) -> dict[str, Any]:
    return control.arm_schedule(
        ceiling=ceiling,
        override_ref=override_ref,
        window_start_minute_utc=9 * 60,
        window_length_minutes=3 * 60,
        is_backup_configured=True,
        is_auto_armed=is_auto_armed,
        now=now,
        live_cron_dir=str(staged.live_cron_dir),
        agent_environment=AgentEnvironment(),
    )


def _start(
    staged: StagedUpdateWorkspace,
    *,
    override_ref: str = "",
    is_backup_configured: bool = True,
    read_agents: AgentsReader | None = None,
    run_in_background: Callable[[Callable[[], None]], None] = run_in_place,
) -> dict[str, Any]:
    return control.start_update(
        ceiling=_CEILING,
        override_ref=override_ref,
        is_backup_configured=is_backup_configured,
        now=_NOW,
        live_cron_dir=str(staged.live_cron_dir),
        proc_root=str(staged.proc_root),
        agent_environment=AgentEnvironment(),
        read_agents=read_agents or staged.read_listed_agents,
        run_in_background=run_in_background,
        clock=lambda: _FINISHED_AT,
    )


def _last_start(staged: StagedUpdateWorkspace) -> dict[str, Any]:
    last_start = control.read_last_start(str(staged.proc_root))
    assert last_start is not None
    return last_start


def _read(
    staged: StagedUpdateWorkspace,
    *,
    is_sign_in_checked: bool = False,
    read_agents: AgentsReader | None = None,
) -> dict[str, Any]:
    return control.read_update_state(
        is_sign_in_checked=is_sign_in_checked,
        proc_root=str(staged.proc_root),
        agent_environment=AgentEnvironment(),
        read_agents=read_agents or staged.read_listed_agents,
    )


def _flag(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv else None


def _expected_cron_line(staged: StagedUpdateWorkspace) -> str:
    return (
        "PATH=/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n"
        "* * * * * root flock -n /run/update-self-dispatch.lock -c "
        f"'cd {staged.root.resolve()} && python3 system/scripts/update_dispatcher_tick.py "
        ">> data/.state/update-apply/dispatcher.log 2>&1'\n"
    )


def _live_cron_file(staged: StagedUpdateWorkspace) -> Path:
    return staged.live_cron_dir / "update-self-dispatch"


def _durable_cron_file(staged: StagedUpdateWorkspace) -> Path:
    return staged.root / "data" / ".state" / "cron.d" / "update-self-dispatch"


def test_arming_writes_the_cron_line_and_the_schedule(
    staged: StagedUpdateWorkspace,
) -> None:
    answer = _arm(staged, override_ref="minds-v0.9.1")

    assert (answer["outcome"], answer["detail"]) == ("ARMED", "")
    assert answer["schedule"] == {
        "schema": 1,
        "armed_at": _NOW,
        "marked_chat": "",
        "marked_at": None,
        "window_seen_open_at": None,
        "pending_skip_reason": "",
        "next_attempt_at": None,
        "last_skip_reason": "",
        "last_skip_at": None,
        "ceiling": _CEILING,
        "window_start_minute_utc": 9 * 60,
        "window_length_minutes": 3 * 60,
        "is_backup_configured": True,
        "override_ref": "minds-v0.9.1",
        "is_auto_armed": False,
    }
    assert staged.read_state("schedule.json") == answer["schedule"]
    # The dispatcher is this tree's own code: nothing is copied in to run.
    assert not staged.state_path("dispatcher.pyz").exists()
    live_line = _live_cron_file(staged).read_text()
    assert live_line == _expected_cron_line(staged)
    assert _durable_cron_file(staged).read_text() == live_line
    assert stat.S_IMODE(_live_cron_file(staged).stat().st_mode) == 0o644
    assert any(
        line.endswith(
            "armed a schedule (180 minutes from 09:00 UTC, target minds-v0.9.1)"
        )
        for line in staged.dispatcher_log_lines()
    )


@pytest.mark.parametrize("is_auto_armed", (True, False))
def test_the_schedule_file_records_whether_the_client_armed_it_by_itself(
    staged: StagedUpdateWorkspace, is_auto_armed: bool
) -> None:
    answer = _arm(staged, is_auto_armed=is_auto_armed)

    assert answer["schedule"]["is_auto_armed"] is is_auto_armed
    assert staged.read_state("schedule.json") == answer["schedule"]


@pytest.mark.parametrize(
    ("has_skill", "is_signed_in", "has_cron", "outcome"),
    [
        (True, True, False, "NO_CRON"),
        (False, True, True, "TOO_OLD_TO_UPDATE"),
        (True, False, True, "NEEDS_SIGN_IN"),
    ],
)
def test_arming_refuses_a_workspace_that_cannot_run_a_scheduled_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    has_skill: bool,
    is_signed_in: bool,
    has_cron: bool,
    outcome: str,
) -> None:
    staged = stage_update_workspace(
        tmp_path,
        monkeypatch,
        has_skill=has_skill,
        is_signed_in=is_signed_in,
        has_cron=has_cron,
    )

    answer = _arm(staged)

    assert answer["outcome"] == outcome
    assert answer["detail"]
    assert answer["schedule"] is None
    assert staged.read_state("schedule.json") is None
    assert not _live_cron_file(staged).exists()
    assert not _durable_cron_file(staged).exists()


def test_a_refused_arm_answers_the_schedule_already_armed_and_leaves_it(
    staged: StagedUpdateWorkspace,
) -> None:
    armed = _arm(staged)["schedule"]
    (staged.root / ".mngr" / "settings.local.toml").unlink()

    answer = _arm(staged, ceiling="minds-v0.9.5", now=_NOW + 60)

    assert answer["outcome"] == "NEEDS_SIGN_IN"
    assert answer["schedule"] == armed
    assert staged.read_state("schedule.json") == armed


def test_arming_where_the_cron_line_cannot_be_written_reads_as_no_cron(
    staged: StagedUpdateWorkspace,
) -> None:
    """The drop-in directory is there, and the line's place in it cannot be written."""
    _live_cron_file(staged).mkdir()

    answer = _arm(staged)

    assert answer["outcome"] == "NO_CRON"
    assert staged.read_state("schedule.json") is None
    assert any(
        "could not install the live cron entry" in line
        for line in staged.dispatcher_log_lines()
    )


def test_re_arming_keeps_the_chat_the_dispatcher_already_started_this_window(
    staged: StagedUpdateWorkspace,
) -> None:
    """A second arm inside the window must not let the dispatcher start a second run."""
    schedule = _arm(staged)["schedule"]
    staged.write_state(
        "schedule.json",
        {
            **schedule,
            "marked_chat": _DISPATCHED_CHAT,
            "marked_at": _NOW - 60,
            "window_seen_open_at": _NOW - 600,
            "pending_skip_reason": "LEASE_HELD",
            "next_attempt_at": _NOW + 600,
            "last_skip_reason": "WINDOW_CLOSED",
            "last_skip_at": _NOW - 86400,
        },
    )

    _arm(staged, now=_NOW + 30)

    rearmed = staged.read_state("schedule.json")
    assert rearmed is not None
    assert (
        rearmed["marked_chat"],
        rearmed["marked_at"],
        rearmed["window_seen_open_at"],
    ) == (_DISPATCHED_CHAT, _NOW - 60, _NOW - 600)
    # The rest of the dispatcher's record starts over.
    assert (
        rearmed["armed_at"],
        rearmed["pending_skip_reason"],
        rearmed["next_attempt_at"],
        rearmed["last_skip_reason"],
        rearmed["last_skip_at"],
    ) == (_NOW + 30, "", None, "", None)


def test_refreshing_an_armed_schedule_rewrites_the_clients_values_and_keeps_the_rest(
    staged: StagedUpdateWorkspace,
) -> None:
    schedule = _arm(staged, override_ref="minds-v0.9.1", is_auto_armed=True)["schedule"]
    marked = {
        **schedule,
        "marked_chat": "update-to-minds-v0-9-1",
        "marked_at": _NOW,
        "last_skip_reason": "LEASE_HELD",
    }
    staged.write_state("schedule.json", marked)
    _live_cron_file(staged).unlink()

    answer = control.refresh_schedule(
        ceiling="minds-v0.10.0",
        window_start_minute_utc=21 * 60 + 30,
        window_length_minutes=4 * 60,
        is_backup_configured=False,
        live_cron_dir=str(staged.live_cron_dir),
    )

    refreshed = {
        **marked,
        "ceiling": "minds-v0.10.0",
        "window_start_minute_utc": 21 * 60 + 30,
        "window_length_minutes": 4 * 60,
        "is_backup_configured": False,
    }
    assert answer == {"is_armed": True, "schedule": refreshed}
    assert staged.read_state("schedule.json") == refreshed
    assert (refreshed["override_ref"], refreshed["is_auto_armed"]) == (
        "minds-v0.9.1",
        True,
    )
    assert _live_cron_file(staged).read_text() == _expected_cron_line(staged)


def test_refreshing_with_the_values_already_there_leaves_the_schedule_file_as_it_is(
    staged: StagedUpdateWorkspace,
) -> None:
    schedule = _arm(staged)["schedule"]
    written = staged.state_path("schedule.json").stat()

    answer = control.refresh_schedule(
        ceiling=_CEILING,
        window_start_minute_utc=9 * 60,
        window_length_minutes=3 * 60,
        is_backup_configured=True,
        live_cron_dir=str(staged.live_cron_dir),
    )

    assert answer == {"is_armed": True, "schedule": schedule}
    assert staged.state_path("schedule.json").stat().st_ino == written.st_ino


def test_refreshing_with_nothing_armed_writes_neither_a_schedule_nor_the_cron_line(
    staged: StagedUpdateWorkspace,
) -> None:
    answer = control.refresh_schedule(
        ceiling=_CEILING,
        window_start_minute_utc=9 * 60,
        window_length_minutes=3 * 60,
        is_backup_configured=True,
        live_cron_dir=str(staged.live_cron_dir),
    )

    assert answer == {"is_armed": False, "schedule": None}
    assert not (staged.root / "data").exists()
    assert not _live_cron_file(staged).exists()


def test_cancelling_deletes_the_schedule_and_says_whether_one_was_there(
    staged: StagedUpdateWorkspace,
) -> None:
    _arm(staged)

    first = control.cancel_schedule()
    second = control.cancel_schedule()

    assert (first, second) == ({"is_cancelled": True}, {"is_cancelled": False})
    assert staged.read_state("schedule.json") is None


def test_skipping_a_version_cancels_the_schedule_and_records_the_skip(
    staged: StagedUpdateWorkspace,
) -> None:
    _arm(staged)

    answer = control.skip_target(target_ref=_CEILING, skipped_at=_NOW)

    recorded = {"schema": 1, "target_ref": _CEILING, "skipped_at": _NOW}
    assert answer == {"is_cancelled": True, "skipped_target": recorded}
    assert staged.read_state("skipped-target.json") == recorded
    assert staged.read_state("schedule.json") is None


@pytest.mark.parametrize(
    ("existing_skipped_at", "kept_target"),
    [
        # Another client's later skip stands.
        (_NOW + 500.0, "minds-v0.9.5"),
        # An earlier one is replaced, and so is one made at the same moment.
        (_NOW - 500.0, _CEILING),
        (_NOW, _CEILING),
    ],
)
def test_of_two_skips_the_later_is_kept_and_the_schedule_is_cancelled_either_way(
    staged: StagedUpdateWorkspace, existing_skipped_at: float, kept_target: str
) -> None:
    _arm(staged)
    staged.write_state(
        "skipped-target.json",
        {
            "schema": 1,
            "target_ref": "minds-v0.9.5",
            "skipped_at": existing_skipped_at,
        },
    )

    answer = control.skip_target(target_ref=_CEILING, skipped_at=_NOW)

    kept = {
        "schema": 1,
        "target_ref": kept_target,
        "skipped_at": max(existing_skipped_at, _NOW),
    }
    assert answer == {"is_cancelled": True, "skipped_target": kept}
    assert staged.read_state("skipped-target.json") == kept
    assert staged.read_state("schedule.json") is None


def test_skipping_with_nothing_armed_still_records_the_skip(
    staged: StagedUpdateWorkspace,
) -> None:
    answer = control.skip_target(target_ref=_CEILING, skipped_at=_NOW)

    assert answer["is_cancelled"] is False
    assert staged.read_state("skipped-target.json") == {
        "schema": 1,
        "target_ref": _CEILING,
        "skipped_at": _NOW,
    }


def test_dismissals_are_recorded_by_the_start_of_the_run_they_dismiss(
    staged: StagedUpdateWorkspace,
) -> None:
    answer = control.record_dismissals(
        note_run_started_at=_NOW - 100.0, outcome_run_started_at=_NOW - 50.0
    )

    recorded = {
        "note_run_started_at": _NOW - 100.0,
        "outcome_run_started_at": _NOW - 50.0,
        "schema": 1,
    }
    assert answer == {"dismissals": recorded}
    assert staged.read_state("dismissals.json") == recorded


def test_a_dismissal_of_a_later_run_stands_over_one_of_an_earlier_run(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state(
        "dismissals.json", {"schema": 1, "note_run_started_at": _NOW + 500.0}
    )

    answer = control.record_dismissals(
        note_run_started_at=_NOW - 100.0, outcome_run_started_at=_NOW - 50.0
    )

    # The note's dismissal kept the file's later run; the outcome's took the one asked for.
    assert answer == {
        "dismissals": {
            "schema": 1,
            "note_run_started_at": _NOW + 500.0,
            "outcome_run_started_at": _NOW - 50.0,
        }
    }
    assert staged.read_state("dismissals.json") == answer["dismissals"]


def test_a_dismissal_that_changes_nothing_writes_nothing(
    staged: StagedUpdateWorkspace,
) -> None:
    nothing_dismissed = control.record_dismissals(
        note_run_started_at=None, outcome_run_started_at=None
    )
    assert nothing_dismissed == {"dismissals": None}
    assert not (staged.root / "data").exists()

    existing = {"schema": 1, "note_run_started_at": _NOW}
    staged.write_state("dismissals.json", existing)
    written = staged.state_path("dismissals.json").stat()

    answer = control.record_dismissals(
        note_run_started_at=_NOW - 100.0, outcome_run_started_at=None
    )

    assert answer == {"dismissals": existing}
    assert staged.state_path("dismissals.json").stat().st_ino == written.st_ino


def test_starting_an_update_answers_at_once_and_records_how_the_create_went(
    staged: StagedUpdateWorkspace,
) -> None:
    queued: list[Callable[[], None]] = []

    answer = _start(staged, run_in_background=queued.append)

    attempt_id = answer["attempt_id"]
    assert answer == {
        "outcome": "STARTING",
        "detail": "",
        "chat_agent_name": "",
        "chat_id": "",
        "attempt_id": attempt_id,
    }
    under_way = {
        "schema": 1,
        "attempt_id": attempt_id,
        "started_at": _NOW,
        "pid": os.getpid(),
        "pid_start_time": "",
        "outcome": None,
        "detail": "",
        "chat_agent_name": "",
        "chat_id": "",
        "finished_at": None,
    }
    assert control.read_last_start(str(staged.proc_root)) == under_way
    assert staged.chat_creates() == []
    # The watchdog's cron line is in place before the chat it watches over exists.
    assert _live_cron_file(staged).read_text() == _expected_cron_line(staged)

    assert len(queued) == 1
    queued[0]()

    assert control.read_last_start(str(staged.proc_root)) == {
        **under_way,
        "outcome": "CREATED",
        "chat_agent_name": STAGED_CREATED_CHAT["name"],
        "chat_id": STAGED_CREATED_CHAT["chat_id"],
        "finished_at": _FINISHED_AT,
    }
    assert len(staged.chat_creates()) == 1


def test_a_second_start_while_the_first_is_creating_its_chat_answers_the_first_attempt(
    staged: StagedUpdateWorkspace,
) -> None:
    queued: list[Callable[[], None]] = []
    first = _start(staged, run_in_background=queued.append)

    second = _start(staged, run_in_background=queued.append)

    assert second == {
        "outcome": "ALREADY_RUNNING",
        "detail": "An update is already starting in this workspace.",
        "chat_agent_name": "",
        "chat_id": "",
        "attempt_id": first["attempt_id"],
    }
    assert len(queued) == 1
    assert _last_start(staged)["attempt_id"] == first["attempt_id"]


def test_an_attempt_whose_process_is_gone_reads_as_failed_and_lets_the_next_start_through(
    staged: StagedUpdateWorkspace, exited_pid: int
) -> None:
    abandoned = {
        "schema": 1,
        "attempt_id": uuid4().hex,
        "started_at": _NOW - 60,
        "pid": exited_pid,
        "pid_start_time": "",
        "outcome": None,
        "detail": "",
        "chat_agent_name": "",
        "chat_id": "",
        "finished_at": None,
    }
    staged.write_state("last-start.json", abandoned)

    assert control.read_last_start(str(staged.proc_root)) == {
        **abandoned,
        "outcome": "CREATE_FAILED",
        "detail": "The process that was starting the update is gone.",
    }
    # Reading says so and leaves the record as the gone process wrote it.
    assert staged.read_state("last-start.json") == abandoned

    answer = _start(staged)

    assert answer["outcome"] == "STARTING"
    assert answer["attempt_id"] != abandoned["attempt_id"]
    assert _last_start(staged)["outcome"] == "CREATED"


def _write_process_start_time(
    staged: StagedUpdateWorkspace, pid: int, start_time: str
) -> None:
    """A ``/proc/<pid>/stat`` whose command name holds spaces and parentheses, as a real one may."""
    fields = ["S"] + [str(number) for number in range(4, 53)]
    fields[19] = start_time
    process_dir = staged.proc_root / str(pid)
    process_dir.mkdir(parents=True, exist_ok=True)
    (process_dir / "stat").write_text(f"{pid} (odd (name) 9 8) {' '.join(fields)}\n")


def test_an_attempt_whose_pid_now_belongs_to_another_process_reads_as_failed(
    staged: StagedUpdateWorkspace,
) -> None:
    """A pid is reused; the time its process started tells the starter from a later process."""
    _write_process_start_time(staged, os.getpid(), "448122")
    queued: list[Callable[[], None]] = []
    first = _start(staged, run_in_background=queued.append)
    assert _last_start(staged)["pid_start_time"] == "448122"
    assert _last_start(staged)["outcome"] is None

    _write_process_start_time(staged, os.getpid(), "991377")

    assert (_last_start(staged)["outcome"], _last_start(staged)["attempt_id"]) == (
        "CREATE_FAILED",
        first["attempt_id"],
    )
    assert _start(staged, run_in_background=queued.append)["outcome"] == "STARTING"


def test_a_create_that_ends_after_a_later_attempt_began_leaves_that_attempts_record(
    staged: StagedUpdateWorkspace,
) -> None:
    _write_process_start_time(staged, os.getpid(), "448122")
    queued: list[Callable[[], None]] = []
    _start(staged, run_in_background=queued.append)
    _write_process_start_time(staged, os.getpid(), "991377")
    second = _start(staged, run_in_background=queued.append)
    under_way = _last_start(staged)

    queued[0]()

    assert len(staged.chat_creates()) == 1
    assert _last_start(staged) == under_way
    assert under_way["attempt_id"] == second["attempt_id"]
    assert under_way["outcome"] is None


def test_a_create_that_raises_closes_its_attempt_out_as_failed(
    staged: StagedUpdateWorkspace,
) -> None:
    """Left with no outcome, the live process's record would hold every later start back."""
    (staged.root / "system" / "scripts" / "message_chat.py").write_text(
        "import sys\nsys.stdout.buffer.write(bytes([0xff, 0xfe, 0xfd]))\n"
    )
    failures: list[ValueError] = []

    def run_and_keep_the_failure(work: Callable[[], None]) -> None:
        try:
            work()
        except UnicodeDecodeError as failure:
            failures.append(failure)

    answer = _start(staged, run_in_background=run_and_keep_the_failure)

    assert len(failures) == 1
    last_start = _last_start(staged)
    assert (
        last_start["attempt_id"],
        last_start["outcome"],
        last_start["finished_at"],
    ) == (answer["attempt_id"], "CREATE_FAILED", _FINISHED_AT)
    assert last_start["detail"].startswith("The update's chat could not be created")
    assert _start(staged, run_in_background=run_and_keep_the_failure)["outcome"] == (
        "STARTING"
    )


def test_starting_creates_the_chat_through_the_templates_messenger_with_the_pinned_message(
    staged: StagedUpdateWorkspace,
) -> None:
    answer = _start(staged)

    assert answer["outcome"] == "STARTING"
    last_start = _last_start(staged)
    assert (
        last_start["outcome"],
        last_start["chat_agent_name"],
        last_start["chat_id"],
    ) == ("CREATED", STAGED_CREATED_CHAT["name"], STAGED_CREATED_CHAT["chat_id"])
    creates = staged.chat_creates()
    assert len(creates) == 1
    assert _flag(creates[0], "--name") == "Update to v0.9.0"
    labels = labels_of(creates[0])
    assert labels[:2] == ["assist=true", "auto_open=true"]
    attempt = staged.read_state("dispatch-attempt.json")
    assert attempt is not None and labels[2:] == [
        f"update_dispatch_attempt={attempt['attempt_id']}"
    ]
    assert creates[0][-1] == snapshot("""\
/update-self

The Imbue Studio app pinned this update, so do not ask it for its version:
- Target: minds-v0.9.0
- Ceiling: minds-v0.9.0
If Step 1 finds uncommitted changes, do not stop the update for them: commit them all as one work-in-progress commit on the current branch, with a message that lists their paths, name that commit in your results, and carry on.
When you resolve the target in Step 2, append `--ceiling minds-v0.9.0` to the `resolve-target` command. If the app cannot be reached for anything else during this run, that is not a fault.\
""")
    dispatch = staged.read_state("last-dispatch.json")
    assert dispatch == {
        "schema": 1,
        "chat_agent_name": STAGED_CREATED_CHAT["name"],
        "chat_id": STAGED_CREATED_CHAT["chat_id"],
        "started_by": "immediate",
        "ceiling": _CEILING,
        "target_ref": _CEILING,
        "dispatched_at": _NOW,
        "attempt_id": attempt["attempt_id"],
    }


def test_an_override_without_backups_carries_both_notes_and_names_the_chat_for_the_override(
    staged: StagedUpdateWorkspace,
) -> None:
    _start(staged, override_ref="upstream/main", is_backup_configured=False)

    create = staged.chat_creates()[0]
    assert _flag(create, "--name") == "Update to upstream/main"
    paragraphs = create[-1].split("\n\n")
    assert paragraphs[1].startswith("The Imbue Studio app pinned this update")
    assert "- Target: upstream/main\n- Ceiling: minds-v0.9.0" in paragraphs[1]
    assert paragraphs[2].startswith(
        "The user chose a specific version in the Imbue Studio app: update this workspace to upstream/main."
    )
    assert paragraphs[3].startswith("This workspace has no backups configured")
    dispatch = staged.read_state("last-dispatch.json")
    assert dispatch is not None
    assert (dispatch["target_ref"], dispatch["ceiling"]) == ("upstream/main", _CEILING)


@pytest.mark.parametrize(
    ("has_skill", "is_signed_in", "outcome", "detail"),
    [
        (
            False,
            True,
            "TOO_OLD_TO_UPDATE",
            "This workspace predates the update-self skill.",
        ),
        (
            True,
            False,
            "NEEDS_SIGN_IN",
            "No AI account is signed in to this workspace.",
        ),
    ],
)
def test_starting_refuses_a_workspace_with_no_skill_or_no_account_before_anything_is_created(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    has_skill: bool,
    is_signed_in: bool,
    outcome: str,
    detail: str,
) -> None:
    staged = stage_update_workspace(
        tmp_path, monkeypatch, has_skill=has_skill, is_signed_in=is_signed_in
    )
    queued: list[Callable[[], None]] = []

    answer = _start(staged, run_in_background=queued.append)

    assert answer == {
        "outcome": outcome,
        "detail": detail,
        "chat_agent_name": "",
        "chat_id": "",
    }
    assert queued == []
    assert control.read_last_start(str(staged.proc_root)) is None
    assert staged.chat_creates() == []


def test_starting_needs_no_cron(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged = stage_update_workspace(tmp_path, monkeypatch, has_cron=False)

    answer = _start(staged)

    assert answer["outcome"] == "STARTING"
    assert _last_start(staged)["outcome"] == "CREATED"
    assert not _durable_cron_file(staged).exists()


def test_a_start_keeps_its_warnings_in_the_dispatcher_log(
    staged: StagedUpdateWorkspace,
) -> None:
    """A route answers its outcome and nothing else, so a warning lives only if the log has it."""
    staged.set_agents([], is_services_agent_listed=False)

    _start(staged)

    assert any(
        line.endswith(
            "mngr list did not list this workspace's services agent; ignoring it"
        )
        for line in staged.dispatcher_log_lines()
    )


def test_a_messenger_whose_own_bare_create_refuses_the_title_hands_the_create_to_the_librarys_bare_create(
    staged: StagedUpdateWorkspace,
) -> None:
    """A released messenger behind an unreachable chat app runs ``mngr create`` under the title it was given, which
    no mngr name can hold; the library's own bare create names the agent so mngr takes it."""
    staged.script_tool(
        "system/scripts/message_chat.py",
        [
            ScriptedToolAnswer(
                exit_code=1,
                stderr="Falling back to `mngr create`: could not connect to the chat app\n"
                "AgentName must be alphanumeric (with dashes and underscores allowed in the middle): "
                "'Update to v0.9.0'\n",
            )
        ],
    )

    _start(staged)

    last_start = _last_start(staged)
    assert (
        last_start["outcome"],
        last_start["chat_agent_name"],
        last_start["chat_id"],
    ) == ("CREATED", "Update-to-v0-9-0", staged.bare_created_agent_id)
    creates = staged.bare_creates()
    assert len(creates) == 1
    assert creates[0][1] == "Update-to-v0-9-0"
    assert "display_name=Update to v0.9.0" in creates[0]


def test_a_chat_app_and_a_bare_create_that_both_fail_read_as_the_chat_app_unreachable(
    staged: StagedUpdateWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged.script_tool(
        "system/scripts/message_chat.py",
        [
            ScriptedToolAnswer(
                exit_code=1,
                stderr="Falling back to `mngr create`: could not connect to the chat app\n",
            )
        ],
    )
    monkeypatch.setenv("STUB_EXIT_CREATE", "1")

    answer = _start(staged)

    assert answer["outcome"] == "STARTING"
    last_start = _last_start(staged)
    assert (last_start["outcome"], last_start["detail"]) == (
        "CHAT_APP_UNREACHABLE",
        "mngr create exited 1",
    )
    assert staged.read_state("last-dispatch.json") is None


def test_a_taken_chat_name_gets_the_next_free_suffix(
    staged: StagedUpdateWorkspace,
) -> None:
    """Earlier runs' chats stay as history, so a second update to the same release needs its own name."""
    staged.set_agents([listed_agent("Update-to-v090", "WAITING")])
    staged.script_tool(
        "system/scripts/message_chat.py",
        [
            ScriptedToolAnswer(
                exit_code=1,
                stderr="The chat app did not create the chat: "
                "A chat named 'Update to v0.9.0 2' already exists; pick another name\n",
            ),
            ScriptedToolAnswer(stdout=json.dumps(STAGED_CREATED_CHAT) + "\n"),
        ],
    )

    _start(staged)

    assert [_flag(create, "--name") for create in staged.chat_creates()] == [
        "Update to v0.9.0 2",
        "Update to v0.9.0 3",
    ]
    assert _last_start(staged)["outcome"] == "CREATED"


def test_starting_answers_a_run_already_under_way_with_its_chat_and_its_start(
    staged: StagedUpdateWorkspace,
) -> None:
    lead_id = f"agent-{uuid4().hex}"
    staged.write_state(
        "run.json", {"chat_agent_name": _RUNNING_CHAT, "started_at": _NOW - 600}
    )
    staged.set_agents([listed_agent(_RUNNING_CHAT, "RUNNING", lead_id)])

    answer = _start(staged)

    assert answer == {
        "outcome": "ALREADY_RUNNING",
        "detail": "An update is already running in this workspace.",
        "chat_agent_name": _RUNNING_CHAT,
        "chat_id": lead_id,
        "run_started_at": _NOW - 600,
    }
    assert staged.chat_creates() == []
    assert control.read_last_start(str(staged.proc_root)) is None


def test_starting_counts_a_run_as_under_way_while_the_agents_cannot_be_read(
    staged: StagedUpdateWorkspace,
) -> None:
    """Unknown agents are never read as no agents: the run's lead may well be there."""
    staged.write_state(
        "run.json", {"chat_agent_name": _RUNNING_CHAT, "started_at": _NOW - 600}
    )

    answer = _start(staged, read_agents=lambda: None)

    assert (answer["outcome"], answer["chat_agent_name"], answer["chat_id"]) == (
        "ALREADY_RUNNING",
        _RUNNING_CHAT,
        "",
    )
    assert staged.chat_creates() == []


def test_starting_reads_the_run_under_way_from_the_observers_events_file(
    staged: StagedUpdateWorkspace,
) -> None:
    """The routes read the agents from the file ``mngr observe`` writes, where a chat carries its id as a label."""
    chat_id = f"agent-{uuid4().hex}"
    staged.write_state(
        "run.json", {"chat_agent_name": _RUNNING_CHAT, "started_at": _NOW - 600}
    )
    write_event_lines(
        staged.events_path,
        [
            full_state_line(
                [
                    observed_services_agent(),
                    observed_agent(
                        f"agent-{uuid4().hex}",
                        _RUNNING_CHAT,
                        "WAITING",
                        chat_id=chat_id,
                    ),
                ],
                _NOW - 30,
            )
        ],
    )

    answer = _start(
        staged,
        read_agents=lambda: read_observed_agents(str(staged.events_path), _NOW),
    )

    assert (answer["outcome"], answer["chat_id"]) == ("ALREADY_RUNNING", chat_id)
    assert not any(call[:1] == ["list"] for call in staged.recorded_calls())


@pytest.mark.parametrize(
    ("lease_text", "holder_state", "outcome"),
    [
        # A lease that names no holder counts as held.
        ("", None, "ALREADY_RUNNING"),
        (
            "\nHeld by another-flow across the update-self pass.\n",
            "RUNNING",
            "ALREADY_RUNNING",
        ),
        # A lease whose holder ended is abandoned, and the new lead takes it over.
        (
            "\nHeld by another-flow across the update-self pass.\n",
            "STOPPED",
            "STARTING",
        ),
    ],
    ids=("no-holder", "live-holder", "ended-holder"),
)
def test_starting_is_held_back_by_an_update_lease_only_while_its_holder_is_there(
    staged: StagedUpdateWorkspace,
    lease_text: str,
    holder_state: str | None,
    outcome: str,
) -> None:
    staged.write_lease("w-1", lease_text)
    staged.set_agents(
        [listed_agent("another-flow", holder_state)] if holder_state is not None else []
    )

    answer = _start(staged)

    assert answer["outcome"] == outcome
    if outcome == "ALREADY_RUNNING":
        assert answer == {
            "outcome": "ALREADY_RUNNING",
            "detail": "Another update flow is working in this workspace.",
            "chat_agent_name": "",
            "chat_id": "",
        }
        assert staged.chat_creates() == []
        assert control.read_last_start(str(staged.proc_root)) is None


def test_starting_answers_a_chat_dispatched_moments_ago_that_has_not_recorded_its_run_yet(
    staged: StagedUpdateWorkspace,
) -> None:
    """Its lead takes the lease and records its start only minutes after its chat is made."""
    chat_id = f"agent-{uuid4().hex}"
    staged.write_state(
        "run.json",
        {
            "chat_agent_name": _RUNNING_CHAT,
            "started_at": _NOW - 86400,
            "verdict": "UPDATED",
        },
    )
    staged.write_state(
        "last-dispatch.json",
        {
            "chat_agent_name": _DISPATCHED_CHAT,
            "chat_id": chat_id,
            "started_by": "scheduled",
            "dispatched_at": _NOW - 120,
        },
    )
    staged.set_agents([listed_agent(_DISPATCHED_CHAT, "RUNNING", chat_id)])

    answer = _start(staged)

    assert answer == {
        "outcome": "ALREADY_RUNNING",
        "detail": "An update is already starting in this workspace.",
        "chat_agent_name": _DISPATCHED_CHAT,
        "chat_id": chat_id,
        # The dispatch's time, not the earlier run's start the record still holds.
        "run_started_at": _NOW - 120,
    }
    assert staged.chat_creates() == []


_DISPATCHED_CHAT_ID = f"agent-{uuid4().hex}"


@pytest.mark.parametrize(
    ("dispatched_chat_id", "listed", "outcome"),
    [
        # Deleted: the create gave it an id, so it was there, and it is no longer listed by name or id.
        (_DISPATCHED_CHAT_ID, [], "STARTING"),
        # Still being created: the chat app answered before it had an id, and it is not listed until it lands.
        ("", [], "ALREADY_RUNNING"),
        # Renamed: listed under its new name, by the id the create gave it.
        (
            _DISPATCHED_CHAT_ID,
            [listed_agent("my-update", "RUNNING", _DISPATCHED_CHAT_ID)],
            "ALREADY_RUNNING",
        ),
        # Ended before it recorded a run: nothing of it is under way.
        (
            _DISPATCHED_CHAT_ID,
            [listed_agent(_DISPATCHED_CHAT, "DONE", _DISPATCHED_CHAT_ID)],
            "STARTING",
        ),
    ],
    ids=("deleted", "still-creating", "renamed", "ended"),
)
def test_starting_counts_a_chat_dispatched_moments_ago_only_while_it_can_still_be_there(
    staged: StagedUpdateWorkspace,
    dispatched_chat_id: str,
    listed: list[dict[str, str]],
    outcome: str,
) -> None:
    staged.write_state(
        "run.json",
        {
            "chat_agent_name": _RUNNING_CHAT,
            "started_at": _NOW - 86400,
            "verdict": "UPDATED",
        },
    )
    staged.write_state(
        "last-dispatch.json",
        {
            "chat_agent_name": _DISPATCHED_CHAT,
            "chat_id": dispatched_chat_id,
            "started_by": "immediate",
            "dispatched_at": _NOW - 120,
        },
    )
    staged.set_agents(listed)

    answer = _start(staged)

    assert answer["outcome"] == outcome
    assert len(staged.chat_creates()) == (1 if outcome == "STARTING" else 0)


def test_a_chat_dispatched_long_ago_that_never_recorded_a_run_does_not_hold_a_start_back(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state(
        "last-dispatch.json",
        {
            "chat_agent_name": _DISPATCHED_CHAT,
            "chat_id": _DISPATCHED_CHAT_ID,
            "started_by": "immediate",
            "dispatched_at": _NOW - 15 * 60,
        },
    )
    staged.set_agents([listed_agent(_DISPATCHED_CHAT, "RUNNING", _DISPATCHED_CHAT_ID)])

    assert _start(staged)["outcome"] == "STARTING"


@pytest.mark.parametrize(
    ("lead_state", "has_cron", "outcome"),
    [
        # The watchdog brings a stopped lead back, so its run is still under way.
        ("STOPPED", True, "ALREADY_RUNNING"),
        ("DONE", True, "ALREADY_RUNNING"),
        # No cron, no watchdog: nothing will bring it back, so it does not hold a new run back.
        ("STOPPED", False, "STARTING"),
        # Its chat was deleted: there is nothing to bring back.
        (None, True, "STARTING"),
    ],
)
def test_starting_counts_a_stopped_lead_as_under_way_only_where_the_watchdog_revives_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lead_state: str | None,
    has_cron: bool,
    outcome: str,
) -> None:
    staged = stage_update_workspace(tmp_path, monkeypatch, has_cron=has_cron)
    staged.write_state(
        "run.json", {"chat_agent_name": _DISPATCHED_CHAT, "started_at": _NOW - 600}
    )
    staged.set_agents(
        [listed_agent(_DISPATCHED_CHAT, lead_state, _DISPATCHED_CHAT_ID)]
        if lead_state is not None
        else []
    )

    answer = _start(staged)

    assert answer["outcome"] == outcome
    if outcome == "ALREADY_RUNNING":
        assert answer["chat_id"] == _DISPATCHED_CHAT_ID
    else:
        assert _last_start(staged)["outcome"] == "CREATED"


@pytest.mark.parametrize(
    ("stderr", "outcome"),
    [
        (
            "The chat app did not create the chat: Connect an AI provider to start chatting.\n",
            "NEEDS_SIGN_IN",
        ),
        (
            "The chat app did not create the chat: mngr create exited 1\n",
            "CREATE_FAILED",
        ),
        # A refusal can run long; the detail keeps its tail.
        (
            "The chat app did not create the chat: " + "a log line\n" * 200,
            "CREATE_FAILED",
        ),
    ],
)
def test_a_create_the_chat_app_refused_is_recorded_as_the_refusal_it_was(
    staged: StagedUpdateWorkspace, stderr: str, outcome: str
) -> None:
    staged.script_tool(
        "system/scripts/message_chat.py",
        [ScriptedToolAnswer(exit_code=1, stderr=stderr)],
    )

    answer = _start(staged)

    assert answer["outcome"] == "STARTING"
    last_start = _last_start(staged)
    assert last_start["outcome"] == outcome
    assert last_start["detail"] == stderr.strip()[-1000:]
    assert (last_start["chat_agent_name"], last_start["chat_id"]) == ("", "")
    assert last_start["finished_at"] == _FINISHED_AT
    assert staged.read_state("last-dispatch.json") is None


def test_a_workspace_with_no_messenger_gets_a_bare_create_carrying_the_display_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged = stage_update_workspace(tmp_path, monkeypatch, has_message_chat=False)

    _start(staged)

    last_start = _last_start(staged)
    assert (last_start["outcome"], last_start["chat_id"]) == (
        "CREATED",
        staged.bare_created_agent_id,
    )
    creates = staged.bare_creates()
    assert len(creates) == 1
    assert creates[0][1] == "Update-to-v0-9-0"
    assert "display_name=Update to v0.9.0" in creates[0]
    assert "auto_open=true" in creates[0]


def test_starting_asks_an_older_templates_account_resolver_once_and_creates_on_its_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged = stage_update_workspace(
        tmp_path, monkeypatch, is_signed_in=False, has_message_chat=False
    )
    scripts_dir = staged.root / "system" / "scripts"
    (scripts_dir / "require_create_account.py").unlink()
    (scripts_dir / "default_account_args.py").write_text("")
    staged.script_program_on_path(
        "uv",
        [ScriptedToolAnswer(stdout="--env\nCLAUDE_CONFIG_DIR=/home/user/acc/a1\n")],
    )

    _start(staged)

    assert _last_start(staged)["outcome"] == "CREATED"
    assert staged.tool_calls("uv") == [
        [
            "run",
            "--no-sync",
            "python",
            "system/scripts/default_account_args.py",
            "claude",
        ]
    ]
    creates = staged.bare_creates()
    assert len(creates) == 1
    assert (
        creates[0][creates[0].index("--env") + 1]
        == "CLAUDE_CONFIG_DIR=/home/user/acc/a1"
    )


def test_reading_a_workspace_with_nothing_armed_answers_nothing_and_writes_nothing(
    staged: StagedUpdateWorkspace,
) -> None:
    answer = _read(staged)

    assert answer == {
        "schedule": None,
        "run": None,
        "last_dispatch": None,
        "run_chat_id": None,
        "is_signed_in": None,
        "skipped_target": None,
        "dismissals": None,
        "last_start": None,
    }
    assert not (staged.root / "data").exists()
    assert not _live_cron_file(staged).exists()


def test_reading_answers_every_record_as_it_stands_and_changes_none(
    staged: StagedUpdateWorkspace,
) -> None:
    _arm(staged)
    _start(staged)
    control.record_dismissals(
        note_run_started_at=_NOW - 100.0, outcome_run_started_at=None
    )
    skipped = {"schema": 1, "target_ref": "minds-v0.8.9", "skipped_at": _NOW - 5.0}
    staged.write_state("skipped-target.json", skipped)
    run = {"chat_agent_name": STAGED_CREATED_CHAT["name"], "started_at": _NOW + 1.0}
    staged.write_state("run.json", run)
    state_dir = staged.state_path("")
    before = {path.name: path.read_bytes() for path in state_dir.iterdir()}

    answer = _read(staged)

    assert answer == {
        "schedule": staged.read_state("schedule.json"),
        "run": run,
        "last_dispatch": staged.read_state("last-dispatch.json"),
        "run_chat_id": None,
        "is_signed_in": None,
        "skipped_target": skipped,
        "dismissals": {"note_run_started_at": _NOW - 100.0, "schema": 1},
        "last_start": staged.read_state("last-start.json"),
    }
    assert answer["schedule"]["ceiling"] == _CEILING
    assert answer["last_start"]["outcome"] == "CREATED"
    assert {path.name: path.read_bytes() for path in state_dir.iterdir()} == before


@pytest.mark.parametrize(
    ("override_ref", "target_ref"), (("", _CEILING), ("minds-v0.8.7", "minds-v0.8.7"))
)
def test_reading_after_a_start_names_the_release_the_run_aimed_at(
    staged: StagedUpdateWorkspace, override_ref: str, target_ref: str
) -> None:
    """A client holds back the release the last dispatch aimed at, so it must read it from the record."""
    _start(staged, override_ref=override_ref)

    assert _read(staged)["last_dispatch"]["target_ref"] == target_ref


@pytest.mark.parametrize(
    (
        "dispatched_chat",
        "listed_chats",
        "is_services_agent_listed",
        "run_chat_id",
        "is_listing_expected",
    ),
    (
        # A run started by hand in a chat: the listing names its chat.
        ("Update-to-v090", ("update-self-1330f8",), True, _DISPATCHED_CHAT_ID, True),
        # Its chat since deleted.
        ("Update-to-v090", (), True, "", True),
        # Agents that cannot be read say nothing about the chat.
        ("Update-to-v090", (), False, None, True),
        # The last dispatch names the run's chat already, so the agents are not read.
        ("update-self-1330f8", ("update-self-1330f8",), True, None, False),
    ),
)
def test_reading_names_the_chat_of_a_run_the_last_dispatch_does_not(
    staged: StagedUpdateWorkspace,
    dispatched_chat: str,
    listed_chats: tuple[str, ...],
    is_services_agent_listed: bool,
    run_chat_id: str | None,
    is_listing_expected: bool,
) -> None:
    staged.write_state(
        "run.json",
        {
            "chat_agent_name": "update-self-1330f8",
            "started_at": 1.0,
            "verdict": "STUCK",
        },
    )
    staged.write_state(
        "last-dispatch.json",
        {"schema": 1, "chat_agent_name": dispatched_chat, "chat_id": "agent-x"},
    )
    staged.set_agents(
        [listed_agent(name, "STOPPED", _DISPATCHED_CHAT_ID) for name in listed_chats],
        is_services_agent_listed=is_services_agent_listed,
    )

    answer = _read(staged)

    assert answer["run_chat_id"] == run_chat_id
    is_listed = any(call[:1] == ["list"] for call in staged.recorded_calls())
    assert is_listed is is_listing_expected


def test_reading_names_no_chat_when_there_is_no_run_or_the_record_names_none(
    staged: StagedUpdateWorkspace,
) -> None:
    def never_read() -> list[dict[str, str]]:
        raise AssertionError("nothing here turns on the workspace's agents")

    assert _read(staged, read_agents=never_read)["run_chat_id"] is None

    staged.write_state("run.json", {"started_at": _NOW, "verdict": "STUCK"})

    assert _read(staged, read_agents=never_read)["run_chat_id"] is None


@pytest.mark.parametrize("is_signed_in", [True, False])
def test_a_read_asked_about_the_account_says_whether_one_is_signed_in_with_nothing_armed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, is_signed_in: bool
) -> None:
    staged = stage_update_workspace(tmp_path, monkeypatch, is_signed_in=is_signed_in)

    asked = _read(staged, is_sign_in_checked=True)
    unasked = _read(staged)

    assert asked["is_signed_in"] is is_signed_in
    assert unasked["is_signed_in"] is None


@pytest.mark.parametrize("skip_field", ["pending_skip_reason", "last_skip_reason"])
def test_a_read_says_whether_an_account_is_signed_in_unasked_once_a_window_was_skipped_for_none(
    staged: StagedUpdateWorkspace, skip_field: str
) -> None:
    schedule = _arm(staged)["schedule"]
    staged.write_state("schedule.json", {**schedule, skip_field: "NEEDS_SIGN_IN"})

    assert _read(staged)["is_signed_in"] is True

    (staged.root / ".mngr" / "settings.local.toml").unlink()

    assert _read(staged)["is_signed_in"] is False


def test_liveness_answers_the_run_the_agents_the_busy_chats_and_the_watchdog_in_one(
    staged: StagedUpdateWorkspace,
) -> None:
    lead_id = f"agent-{uuid4().hex}"
    chat_id = f"agent-{uuid4().hex}"
    run = {"chat_agent_name": _RUNNING_CHAT, "started_at": _NOW - 600}
    staged.write_state("run.json", run)
    staged.set_agents(
        [{**listed_agent(_RUNNING_CHAT, "WAITING", lead_id), "chat_id": chat_id}]
    )
    staged.script_tool(
        "system/scripts/background_tasks.py",
        [
            ScriptedToolAnswer(
                stdout=f"{chat_id}\t{lead_id}\t{_RUNNING_CHAT}\ttrue\t2\n"
            )
        ],
    )
    _arm(staged)

    liveness = control.read_liveness(
        live_cron_dir=str(staged.live_cron_dir),
        agent_environment=AgentEnvironment(),
        read_agents=staged.read_listed_agents,
    )

    assert liveness == {
        "run": run,
        "agents": [
            {
                "name": "system-services",
                "state": "RUNNING",
                "agent_id": SERVICES_AGENT_ID,
                "chat_id": "",
            },
            {
                "name": _RUNNING_CHAT,
                "state": "WAITING",
                "agent_id": lead_id,
                "chat_id": chat_id,
            },
        ],
        "busy_chats": [
            {
                "chat_id": chat_id,
                "agent_id": lead_id,
                "agent_name": _RUNNING_CHAT,
                "is_busy": True,
            }
        ],
        "is_watchdog_installed": True,
    }


def test_liveness_answers_unknown_agents_as_unknown_and_not_as_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged = stage_update_workspace(tmp_path, monkeypatch, has_cron=False)

    liveness = control.read_liveness(
        live_cron_dir=str(staged.live_cron_dir),
        agent_environment=AgentEnvironment(),
        read_agents=lambda: read_observed_agents(str(staged.events_path), _NOW),
    )

    assert liveness == {
        "run": None,
        "agents": None,
        "busy_chats": [],
        "is_watchdog_installed": False,
    }


def test_liveness_reads_the_agents_states_and_chats_from_the_observers_events_file(
    staged: StagedUpdateWorkspace,
) -> None:
    lead_id = f"agent-{uuid4().hex}"
    chat_id = f"agent-{uuid4().hex}"
    write_event_lines(
        staged.events_path,
        [
            full_state_line(
                [
                    observed_services_agent(),
                    observed_agent(lead_id, _RUNNING_CHAT, "RUNNING", chat_id=chat_id),
                ],
                _NOW - 30,
            )
        ],
    )

    liveness = control.read_liveness(
        live_cron_dir=str(staged.live_cron_dir),
        agent_environment=AgentEnvironment(),
        read_agents=lambda: read_observed_agents(str(staged.events_path), _NOW),
    )

    assert liveness["agents"] == [
        {
            "name": "system-services",
            "state": "RUNNING",
            "agent_id": SERVICES_AGENT_ID,
            "chat_id": "",
        },
        {
            "name": _RUNNING_CHAT,
            "state": "RUNNING",
            "agent_id": lead_id,
            "chat_id": chat_id,
        },
    ]
    assert liveness["is_watchdog_installed"] is False
