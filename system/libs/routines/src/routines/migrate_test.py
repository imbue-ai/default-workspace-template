from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest
from inline_snapshot import snapshot

from routines.definitions import FileDefinitionStore
from routines.migrate import ConvertedEntry
from routines.migrate import ENV_WRAPPER_PATH
from routines.migrate import LEGACY_ENV_WRAPPER_PATH
from routines.migrate import SkippedEntry
from routines.migrate import convert_cron_entry
from routines.migrate import migrate_cron_entries
from routines.mock_repository_test import RecordingRepository
from routines.paths import RoutinesPaths
from routines.primitives import RoutineId
from routines.primitives import Weekday
from routines.state import FileStateStore
from routines.testing import LEGACY_RUN_JOB_PATH
from routines.testing import legacy_runner_line

WRAPPER = LEGACY_ENV_WRAPPER_PATH
RUN_JOB = LEGACY_RUN_JOB_PATH
RUN_AUTOMATION = "/home/user/workspace/system/libs/automations/run_automation.sh"


def test_a_runner_entry_becomes_a_command_routine_with_its_cadence_and_hour() -> None:
    line = f"* * * * *   root   {WRAPPER} {RUN_JOB} news-sync --every 1d --at 7 --retry-after 5m python3 sync.py --all >> /var/log/supervisor/news.log 2>&1\n"
    (converted,) = convert_cron_entry(line, "news-sync")
    assert isinstance(converted, ConvertedEntry)
    assert converted.routine_id == RoutineId("news-sync")
    assert converted.definition.model_dump(mode="json") == snapshot(
        {
            "title": "News sync",
            "what": "python3 sync.py --all",
            "every": "1d",
            "at": "07:00",
            "days": [],
            "day_of_month": None,
            "once": None,
            "command": ["python3", "sync.py", "--all"],
            "skill": None,
            "enabled": True,
            "timeout": "1h",
            "retry_after": "5m",
            "notify_after_failures": 3,
            "app": None,
        }
    )


def test_a_redirect_written_without_a_space_is_not_part_of_the_command() -> None:
    line = f"* * * * * root {WRAPPER} {RUN_JOB} news-sync --every 6h python3 sync.py >>/var/log/x.log 2>/dev/null\n"
    (converted,) = convert_cron_entry(line, "news-sync")
    assert isinstance(converted, ConvertedEntry)
    assert converted.definition.command == ("python3", "sync.py")


def test_an_automation_entry_becomes_a_skill_routine() -> None:
    line = f"* * * * *   root   {WRAPPER} {RUN_JOB} news --every 1d --at 9 bash {RUN_AUTOMATION} news >> /var/log/supervisor/news-job.log 2>&1\n"
    (converted,) = convert_cron_entry(line, "news")
    assert isinstance(converted, ConvertedEntry)
    assert converted.definition.skill == "news"
    assert converted.definition.command is None


def test_an_automation_entry_with_the_scripts_options_still_names_the_skill() -> None:
    line = f"* * * * * root {WRAPPER} {RUN_JOB} news --every 1d bash {RUN_AUTOMATION} --template caretaker --type claude news\n"
    (converted,) = convert_cron_entry(line, "news")
    assert isinstance(converted, ConvertedEntry)
    assert converted.definition.skill == "news"


def test_a_runner_job_id_with_capitals_and_underscores_becomes_a_hyphenated_routine_id() -> None:
    line = f"* * * * * root {WRAPPER} {RUN_JOB} News_Sync --every 6h python3 sync.py\n"
    (converted,) = convert_cron_entry(line, "News_Sync")
    assert isinstance(converted, ConvertedEntry)
    assert converted.routine_id == RoutineId("news-sync")
    # The retired runner kept the job's state under the name as the line spelled it.
    assert converted.legacy_job_name == "News_Sync"


def test_the_caretaker_entry_is_recognized_as_the_switch_on_the_shipped_routine() -> None:
    line = f"* * * * *   root   {WRAPPER} {RUN_JOB} caretaker --every 7d --at 3 bash /home/user/workspace/system/services/caretaker/caretaker_check.sh >> /var/log/supervisor/caretaker-job.log 2>&1\n"
    (converted,) = convert_cron_entry(line, "minds-caretaker")
    assert isinstance(converted, ConvertedEntry)
    assert converted.is_caretaker_switch


@pytest.mark.parametrize(
    ("line", "every", "at", "days"),
    [
        ("*/15 * * * * root cmd", "15m", None, ()),
        ("0 */2 * * * root cmd", "2h", None, ()),
        ("30 9 * * * root cmd", "1d", "09:30", ()),
        ("30 9 * * 1 root cmd", "1d", "09:30", (Weekday.MON,)),
        ("30 9 * * 1-5 root cmd", "1d", "09:30", (Weekday.MON, Weekday.TUE, Weekday.WED, Weekday.THU, Weekday.FRI)),
        ("0 8 * * 1,3,5 root cmd", "1d", "08:00", (Weekday.MON, Weekday.WED, Weekday.FRI)),
        ("0 8 * * 0,7 root cmd", "1d", "08:00", (Weekday.SUN,)),
        ("0 0 1 * * root cmd", "1mo", "00:00", ()),
    ],
)
def test_plain_cron_lines_in_the_shapes_a_definition_can_say_are_converted(
    line: str, every: str, at: str | None, days: tuple[Weekday, ...]
) -> None:
    (converted,) = convert_cron_entry(line + "\n", "weekly_report")
    assert isinstance(converted, ConvertedEntry)
    assert str(converted.definition.every) == every
    assert converted.definition.at == at
    assert converted.definition.days == days
    assert converted.routine_id == RoutineId("weekly-report")


def test_a_cron_line_naming_a_month_is_left_with_that_reason() -> None:
    (skipped,) = convert_cron_entry("0 0 1 6 * root cmd\n", "june")
    assert isinstance(skipped, SkippedEntry)
    assert skipped.reason == "a schedule naming a month has no routine form"


@pytest.mark.parametrize("weekday_field", ["*/2", "mon-fri", "1-8", "5-", "5-1"])
def test_a_weekday_field_with_no_routine_form_is_left_with_a_reason(weekday_field: str) -> None:
    (skipped,) = convert_cron_entry(f"0 8 * * {weekday_field} root cmd\n", "odd")
    assert isinstance(skipped, SkippedEntry)
    assert skipped.reason == "the schedule has no routine form"


def test_a_cron_field_a_duration_or_a_time_of_day_refuses_leaves_the_line_with_its_reason() -> None:
    """One odd line is skipped with the refusal's words, never a crash that would stop the whole pass."""
    (every_zero,) = convert_cron_entry("*/0 * * * * root cmd\n", "odd")
    assert isinstance(every_zero, SkippedEntry)
    assert "0m" in every_zero.reason
    (hour_24,) = convert_cron_entry("0 24 * * * root cmd\n", "odd")
    assert isinstance(hour_24, SkippedEntry)
    assert "24:00" in hour_24.reason


def test_migration_writes_definitions_carries_state_over_and_removes_both_copies(
    definitions: FileDefinitionStore, states: FileStateStore, legacy_cron_dir: Path, live_cron_dir: Path
) -> None:
    root = definitions.paths.repo_root
    durable, live = legacy_cron_dir, live_cron_dir
    entry = legacy_runner_line("news-sync", "6h", "python3", "sync.py")
    (durable / "news-sync").write_text(entry)
    (live / "news-sync").write_text(entry)
    caretaker = f"* * * * * root {WRAPPER} {RUN_JOB} caretaker --every 7d --at 3 bash /x/caretaker_check.sh >> /var/log/c.log 2>&1\n"
    (durable / "minds-caretaker").write_text(caretaker)
    (durable / "june").write_text("0 0 1 6 * root cmd\n")
    job_state = root / "data" / ".state" / "jobs" / "news-sync"
    job_state.mkdir(parents=True)
    (job_state / "last_success").write_text("1760000000\n")
    (job_state / "failures").write_text("2\n")

    report = migrate_cron_entries(root, definitions, states, live_cron_dir=live)

    assert report.converted == (RoutineId("news-sync"),)
    assert report.enabled_caretaker is True
    assert [skipped.file_name for skipped in report.skipped] == ["june"]
    assert definitions.load(RoutineId("news-sync")).definition is not None
    assert not (durable / "news-sync").exists()
    assert not (live / "news-sync").exists()
    assert not (durable / "minds-caretaker").exists()
    assert (durable / "june").exists()
    state = states.read(RoutineId("news-sync"))
    assert state.last_succeeded_at == datetime.fromtimestamp(1760000000, tz=timezone.utc)
    assert state.consecutive_failures == 2
    assert states.read(RoutineId("caretaker")).is_enabled is True

    again = migrate_cron_entries(root, definitions, states, live_cron_dir=live)
    assert again.converted == ()


def test_an_entry_whose_id_is_already_a_routine_stays_in_place(
    definitions: FileDefinitionStore, states: FileStateStore, legacy_cron_dir: Path, live_cron_dir: Path
) -> None:
    root = definitions.paths.repo_root
    durable = legacy_cron_dir
    entry = legacy_runner_line("news-sync", "6h", "python3", "sync.py")
    (durable / "news-sync").write_text(entry)
    first = migrate_cron_entries(root, definitions, states, live_cron_dir=live_cron_dir)
    assert first.converted == (RoutineId("news-sync"),)
    (durable / "news-sync").write_text(entry)

    again = migrate_cron_entries(root, definitions, states, live_cron_dir=live_cron_dir)

    assert again.converted == ()
    assert [(skipped.file_name, skipped.reason) for skipped in again.skipped] == [
        ("news-sync", "the routine news-sync exists")
    ]
    assert (durable / "news-sync").exists()


def test_an_entry_whose_lines_would_be_one_routine_is_left_whole_with_nothing_written(
    definitions: FileDefinitionStore, states: FileStateStore, legacy_cron_dir: Path, live_cron_dir: Path
) -> None:
    """A file converts whole or not at all: no definition is written beside a cron line that stays."""
    root = definitions.paths.repo_root
    durable = legacy_cron_dir
    (durable / "reports").write_text("0 8 * * * root cmd-one\n0 9 * * * root cmd-two\n")

    report = migrate_cron_entries(root, definitions, states, live_cron_dir=live_cron_dir)

    assert report.converted == ()
    assert [(skipped.file_name, skipped.reason) for skipped in report.skipped] == [
        ("reports", "two of its lines would both be the routine reports")
    ]
    assert (durable / "reports").exists()
    assert definitions.list_routines() == []


def test_an_entry_left_in_place_is_pointed_at_the_moved_env_wrapper_in_both_copies(
    definitions: FileDefinitionStore, states: FileStateStore, legacy_cron_dir: Path, live_cron_dir: Path
) -> None:
    root = definitions.paths.repo_root
    durable, live = legacy_cron_dir, live_cron_dir
    entry = f"0 0 1 6 * root {WRAPPER} bash scripts/june_report.sh >> /var/log/supervisor/june.log 2>&1\n"
    (durable / "june").write_text(entry)
    (live / "june").write_text(entry)

    report = migrate_cron_entries(root, definitions, states, live_cron_dir=live)

    assert [skipped.file_name for skipped in report.skipped] == ["june"]
    expected = entry.replace(WRAPPER, ENV_WRAPPER_PATH)
    assert (durable / "june").read_text() == expected
    assert (live / "june").read_text() == expected


def test_an_entry_whose_commit_is_refused_is_still_converted_with_its_state(
    paths: RoutinesPaths, states: FileStateStore, legacy_cron_dir: Path, live_cron_dir: Path
) -> None:
    root = paths.repo_root
    durable = legacy_cron_dir
    (durable / "news-sync").write_text(legacy_runner_line("news-sync", "6h", "python3", "sync.py"))
    job_state = root / "data" / ".state" / "jobs" / "news-sync"
    job_state.mkdir(parents=True)
    (job_state / "failures").write_text("2\n")
    definitions = FileDefinitionStore(paths=paths, repository=RecordingRepository(repo_root=root, is_refusing=True))

    report = migrate_cron_entries(root, definitions, states, live_cron_dir=live_cron_dir)

    assert report.converted == (RoutineId("news-sync"),)
    assert definitions.load(RoutineId("news-sync")).definition is not None
    assert states.read(RoutineId("news-sync")).consecutive_failures == 2
    assert not (durable / "news-sync").exists()
