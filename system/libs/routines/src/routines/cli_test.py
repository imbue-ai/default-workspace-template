import json
import subprocess
from datetime import timedelta
from pathlib import Path

from click.testing import CliRunner
from click.testing import Result

from imbue.imbue_common.model_update import to_update
from routines.cli import DEFAULT_WAKE_TIMEOUT
from routines.cli import LEAST_WAKE_WAIT
from routines.cli import WAKE_MARGIN_BEFORE_RUN_DEADLINE
from routines.cli import routines
from routines.cli import run_routine_of
from routines.cli import wait_within_run
from routines.cli import wake_defaults
from routines.cli import wake_run_result
from routines.data_types import CurrentRun
from routines.data_types import RoutineDefinition
from routines.data_types import RunResult
from routines.data_types import WakeOutcome
from routines.migrate import LEGACY_ENV_WRAPPER_PATH
from routines.migrate import LIVE_CRON_DIR_ENV
from routines.mock_http_server_test import FakeHttpServer
from routines.paths import REPO_ROOT_ENV
from routines.paths import RoutinesPaths
from routines.primitives import RoutineId
from routines.primitives import RunId
from routines.primitives import RunOutcome
from routines.primitives import RunTrigger
from routines.primitives import RunWarning
from routines.runner import ROUTINE_ID_ENV
from routines.runner import RUN_DIR_ENV
from routines.runner import RUN_ID_ENV
from routines.state import FileStateStore
from routines.testing import TEST_NOW
from routines.testing import legacy_runner_line
from routines.testing import wait_for_last_run


def _invoke(paths: RoutinesPaths, *args: str, environ: dict[str, str] | None = None) -> Result:
    runner = CliRunner()
    env = {REPO_ROOT_ENV: str(paths.repo_root), **(environ or {})}
    return runner.invoke(routines, list(args), env=env, catch_exceptions=False)


def _add_command_routine(paths: RoutinesPaths, *command: str, every: str = "1h", **flags: str) -> Result:
    """``routines add x`` with a placeholder title and sentence, the cadence, any other flag, and the command."""
    flag_args = [arg for name, value in flags.items() for arg in (f"--{name.replace('_', '-')}", value)]
    return _invoke(paths, "add", "x", "--title", "X", "--what", "Y", "--every", every, *flag_args, "--", *command)


def test_add_list_show_edit_and_remove_round_trip(git_paths: RoutinesPaths) -> None:
    added = _invoke(
        git_paths,
        "add",
        "news-sync",
        "--title",
        "News sync",
        "--what",
        "Pulls news.",
        "--every",
        "6h",
        "--",
        "python3",
        "sync.py",
    )
    assert added.exit_code == 0, added.output
    assert (git_paths.definitions_dir / "news-sync.toml").is_file()
    assert (
        subprocess.run(
            ["git", "log", "--format=%s", "-1"], cwd=git_paths.repo_root, capture_output=True, text=True
        ).stdout.strip()
        == "routines: add news-sync"
    )

    listed = _invoke(git_paths, "list", "--format", "json")
    listing = json.loads(listed.output)
    assert [row["routine_id"] for row in listing["routines"]] == ["news-sync"]
    assert listing["routines"][0]["health"] == "NOT_RUN_YET"
    assert listing["routines"][0]["schedule_text"] == "Every 6 hours"

    shown = _invoke(git_paths, "show", "news-sync")
    assert "News sync (news-sync)" in shown.output
    assert "Every 6 hours" in shown.output

    edited = _invoke(git_paths, "edit", "news-sync", "--every", "1d", "--at", "07:30", "--days", "mon,fri")
    assert edited.exit_code == 0, edited.output
    assert "Every Monday and Friday at 7:30 am" in _invoke(git_paths, "show", "news-sync").output

    # The kept time of day refuses a cadence under a day; an empty value clears it.
    kept = _invoke(git_paths, "edit", "news-sync", "--every", "2h")
    assert kept.exit_code == 1
    assert "a day or longer" in kept.output
    cleared = _invoke(git_paths, "edit", "news-sync", "--every", "2h", "--at", "", "--days", "")
    assert cleared.exit_code == 0, cleared.output
    assert "Every 2 hours" in _invoke(git_paths, "show", "news-sync").output

    removed = _invoke(git_paths, "remove", "news-sync")
    assert removed.exit_code == 0
    assert not (git_paths.definitions_dir / "news-sync.toml").exists()
    assert "No routines yet" in _invoke(git_paths, "list").output


def test_add_refuses_a_definition_that_does_not_validate_with_the_reason(git_paths: RoutinesPaths) -> None:
    result = _add_command_routine(git_paths, "true", every="15m", at="07:00")
    assert result.exit_code == 1
    assert "a day or longer" in result.output


def test_add_refuses_a_weekday_or_a_moment_it_cannot_read_in_the_commands_words(git_paths: RoutinesPaths) -> None:
    bad_day = _add_command_routine(git_paths, "true", every="1d", days="funday")
    assert bad_day.exit_code == 1
    assert "error: days: 'funday' is not a comma separated list of mon to sun" in bad_day.output

    bad_moment = _invoke(git_paths, "add", "x", "--title", "X", "--what", "Y", "--once", "tomorrow", "--", "true")
    assert bad_moment.exit_code == 1
    assert "error: once: 'tomorrow' is not a moment written as YYYY-MM-DDTHH:MM" in bad_moment.output


def test_add_refuses_both_members_of_an_exclusive_pair(git_paths: RoutinesPaths) -> None:
    both_schedules = _add_command_routine(git_paths, "true", once="2026-11-03T15:00")
    assert both_schedules.exit_code == 1
    assert "--every so often or --once at a moment, not both" in both_schedules.output
    both_actions = _add_command_routine(git_paths, "true", skill="digest")
    assert both_actions.exit_code == 1
    assert "a --skill or a command after --, not both" in both_actions.output
    assert not (git_paths.definitions_dir / "x.toml").exists()


def test_add_needs_a_command_or_a_skill(git_paths: RoutinesPaths) -> None:
    result = _invoke(git_paths, "add", "x", "--title", "X", "--what", "Y", "--every", "1h")
    assert result.exit_code == 1
    assert "exactly one of 'command' and 'skill'" in result.output


def test_disable_and_enable_flip_the_switch_in_the_state(git_paths: RoutinesPaths) -> None:
    _add_command_routine(git_paths, "true")
    assert _invoke(git_paths, "disable", "x").exit_code == 0
    listing = json.loads(_invoke(git_paths, "list", "--format", "json").output)
    assert listing["routines"][0]["health"] == "OFF"
    assert _invoke(git_paths, "enable", "x").exit_code == 0
    listing = json.loads(_invoke(git_paths, "list", "--format", "json").output)
    assert listing["routines"][0]["health"] == "NOT_RUN_YET"


def test_run_with_wait_runs_in_process_and_reports_the_outcome(git_paths: RoutinesPaths) -> None:
    _add_command_routine(git_paths, "sh", "-c", "echo ran; exit 0")
    result = _invoke(git_paths, "run", "x", "--wait")
    assert result.exit_code == 0, result.output
    receipt = json.loads(result.output.splitlines()[0])
    assert receipt["routine_id"] == "x"
    assert "ran" in _invoke(git_paths, "log", "x").output
    runs = json.loads(_invoke(git_paths, "runs", "x", "--format", "json").output)
    assert runs[0]["outcome"] == "SUCCEEDED"
    assert runs[0]["run_id"] == receipt["run_id"]


def test_a_failed_run_with_wait_exits_one(git_paths: RoutinesPaths) -> None:
    _add_command_routine(git_paths, "sh", "-c", "exit 2")
    assert _invoke(git_paths, "run", "x", "--wait").exit_code == 1


def test_run_with_a_mistyped_run_id_says_so(git_paths: RoutinesPaths) -> None:
    _add_command_routine(git_paths, "true")
    result = _invoke(git_paths, "run", "x", "--wait", "--run-id", "not-a-run")
    assert result.exit_code == 1
    assert "There is no run called 'not-a-run'" in result.output


def test_tick_names_what_it_started_and_says_nothing_when_nothing_is_due(git_paths: RoutinesPaths) -> None:
    _add_command_routine(git_paths, "true")
    assert _invoke(git_paths, "disable", "x").exit_code == 0
    assert _invoke(git_paths, "tick").stdout == ""

    # A switched-on routine that never ran is due at once; the run it spawns is waited for so nothing leaks.
    assert _invoke(git_paths, "enable", "x").exit_code == 0
    tick = _invoke(git_paths, "tick")
    assert tick.exit_code == 0
    assert tick.stdout == "started x\n"
    wait_for_last_run(FileStateStore(paths=git_paths), RoutineId("x"), 30)


def test_migrate_cron_prints_what_it_converted_and_what_it_left(
    git_paths: RoutinesPaths, legacy_cron_dir: Path, live_cron_dir: Path
) -> None:
    wrapper = LEGACY_ENV_WRAPPER_PATH
    converted_line = legacy_runner_line("news-sync", "6h", "python3", "sync.py")
    (legacy_cron_dir / "news-sync").write_text(converted_line)
    (live_cron_dir / "news-sync").write_text(converted_line)
    (legacy_cron_dir / "june").write_text(f"0 0 1 6 * root {wrapper} cmd\n")
    environ = {LIVE_CRON_DIR_ENV: str(live_cron_dir)}

    result = _invoke(git_paths, "migrate-cron", environ=environ)

    assert result.exit_code == 0
    assert result.stdout == "converted news-sync\nleft june in place: a schedule naming a month has no routine form\n"
    assert "news-sync" in _invoke(git_paths, "list").output
    # The live copy of a converted entry goes with the durable one; the machine's own /etc/cron.d is never touched.
    assert not (live_cron_dir / "news-sync").exists()
    assert _invoke(git_paths, "migrate-cron", environ=environ).stdout == (
        "left june in place: a schedule naming a month has no routine form\n"
    )


def test_show_of_a_missing_routine_says_so(git_paths: RoutinesPaths) -> None:
    result = _invoke(git_paths, "show", "nothing")
    assert result.exit_code == 1
    assert "There is no routine called 'nothing'" in result.output


def test_log_of_a_missing_routine_or_a_mistyped_run_id_says_so(git_paths: RoutinesPaths) -> None:
    missing = _invoke(git_paths, "log", "nothing")
    assert missing.exit_code == 1
    assert "There is no routine called 'nothing'" in missing.output

    _invoke(git_paths, "add", "hello", "--title", "Hello", "--what", "Says hello.", "--every", "1d", "--", "true")
    mistyped = _invoke(git_paths, "log", "hello", "not-a-run")
    assert mistyped.exit_code == 1
    assert "There is no run called 'not-a-run'" in mistyped.output

    never_ran = RunId.generate()
    unknown = _invoke(git_paths, "log", "hello", str(never_ran))
    assert unknown.exit_code == 1
    assert f"There is no run called '{never_ran}'" in unknown.output


def test_a_wake_takes_its_title_and_timeout_from_the_routine_unless_told_otherwise() -> None:
    definition = RoutineDefinition.model_validate(
        {"title": "Weekly check-up", "what": "Checks.", "every": "1w", "command": ["true"], "timeout": "2h"}
    )
    caretaker = RoutineId("caretaker")

    from_definition = wake_defaults(definition, caretaker, None, None)
    assert (from_definition.title, str(from_definition.timeout)) == ("Weekly check-up", "2h")

    from_flags = wake_defaults(definition, caretaker, "Check-up", "30m")
    assert (from_flags.title, str(from_flags.timeout)) == ("Check-up", "30m")

    undefined = wake_defaults(None, caretaker, None, None)
    assert (undefined.title, str(undefined.timeout)) == ("Caretaker", DEFAULT_WAKE_TIMEOUT)


def test_a_wake_inside_a_run_stops_before_the_runs_own_deadline() -> None:
    run_id = RunId.generate()
    started_ten_minutes_ago = CurrentRun(
        run_id=run_id, pid=1, trigger=RunTrigger.SCHEDULED, started_at=TEST_NOW - timedelta(minutes=10)
    )
    timeout = timedelta(hours=2)

    wait = wait_within_run(started_ten_minutes_ago, run_id, timeout, timeout, TEST_NOW)
    assert wait == timeout - timedelta(minutes=10) - WAKE_MARGIN_BEFORE_RUN_DEADLINE
    # A shorter wait (a --timeout flag) is kept; a longer one is capped at what is left of the run.
    assert wait_within_run(started_ten_minutes_ago, run_id, timeout, timedelta(minutes=10), TEST_NOW) == timedelta(
        minutes=10
    )
    assert wait_within_run(started_ten_minutes_ago, run_id, timeout, timedelta(hours=3), TEST_NOW) == wait

    nearly_over = CurrentRun(run_id=run_id, pid=1, trigger=RunTrigger.SCHEDULED, started_at=TEST_NOW - timeout)
    assert wait_within_run(nearly_over, run_id, timeout, timeout, TEST_NOW) == LEAST_WAKE_WAIT
    # A state that names another run, or none, says nothing about this one.
    assert wait_within_run(None, run_id, timeout, timeout, TEST_NOW) == timeout
    assert wait_within_run(started_ten_minutes_ago, RunId.generate(), timeout, timeout, TEST_NOW) == timeout


def test_the_run_a_wake_is_inside_is_the_one_its_environment_names() -> None:
    caretaker = RoutineId("caretaker")
    run_id = RunId.generate()
    assert run_routine_of(None, {ROUTINE_ID_ENV: "nightly-check"}, caretaker) is None
    assert run_routine_of(run_id, {ROUTINE_ID_ENV: "nightly-check"}, caretaker) == RoutineId("nightly-check")
    # A run that did not name its routine is taken to be the wanted routine's own.
    assert run_routine_of(run_id, {}, caretaker) == caretaker


def test_a_wake_leaves_its_chat_on_the_run_only_when_the_chat_is_the_runs_own_routines() -> None:
    outcome = WakeOutcome(
        outcome=RunOutcome.TIMED_OUT,
        summary="Checked.",
        chat_id="agent-1",
        warnings=(RunWarning.RESTARTED_PREVIOUS_RUN,),
    )
    own = wake_run_result(outcome, RoutineId("caretaker"), "caretaker")
    assert (own.summary, own.chat_id) == ("Checked.", "agent-1")
    # How the wake ended, and what it had to say, are the run's too.
    assert (own.outcome, own.warnings) == (RunOutcome.TIMED_OUT, (RunWarning.RESTARTED_PREVIOUS_RUN,))
    # A command routine that wakes another routine's chat does not adopt that chat.
    other = wake_run_result(outcome, RoutineId("caretaker"), "nightly-check")
    assert (other.summary, other.chat_id) == ("Checked.", None)


def test_wake_refuses_a_timeout_that_is_not_a_duration_in_the_commands_words(git_paths: RoutinesPaths) -> None:
    result = _invoke(git_paths, "wake", "caretaker", "--timeout", "soon")
    assert result.exit_code == 1
    assert "error: 'soon' is not a duration" in result.output


def test_a_wake_inside_a_run_wakes_the_routines_chat_and_leaves_its_result_for_the_run(
    git_paths: RoutinesPaths, http_server: FakeHttpServer
) -> None:
    """What the Caretaker's check runs: the chat is woken through the chat app the registry names, and the
    agent's answer and the chat go into the run directory's result.json for the runner to record."""
    routine_id = RoutineId("digest")
    registry = git_paths.repo_root / "data" / ".state" / "apps.toml"
    registry.parent.mkdir(parents=True)
    registry.write_text(f'[[apps]]\nname = "chat"\nurl = "{http_server.url}"\n')
    FileStateStore(paths=git_paths).update(
        routine_id, lambda state: state.model_copy_update(to_update(state.field_ref().chat_id, "c1"))
    )
    http_server.answer("GET", "/api/chats", (200, {"chats": [{"chat_id": "c1", "status": "idle"}]}))
    http_server.answer("POST", "/api/chats/c1/message", (200, {}))
    http_server.answer("GET", "/api/chats/c1/events?limit=1", (200, {"events": [{"event_id": "e1"}]}))
    http_server.answer(
        "GET",
        "/api/chats/c1/events?after=e1&limit=200",
        (200, {"events": [{"event_id": "e2", "type": "assistant_message", "text": "Done: 3 summarized.\nMore."}]}),
    )
    run_id = RunId.generate()
    run_dir = git_paths.run_dir(routine_id, run_id)
    run_dir.mkdir(parents=True)

    result = _invoke(
        git_paths,
        "wake",
        "digest",
        environ={RUN_ID_ENV: str(run_id), ROUTINE_ID_ENV: str(routine_id), RUN_DIR_ENV: str(run_dir)},
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == "succeeded: Done: 3 summarized.\n"
    sent = [request.body["message"] for request in http_server.requests if request.method == "POST" and request.body]
    assert sent == ["/clear", "/digest"]
    written = RunResult.model_validate_json((run_dir / "result.json").read_text())
    assert written == RunResult(summary="Done: 3 summarized.", chat_id="c1", outcome=RunOutcome.SUCCEEDED)
