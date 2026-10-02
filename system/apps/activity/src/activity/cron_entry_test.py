from pathlib import Path

from activity.cron_entry import CRON_ENTRY_NAME
from activity.cron_entry import cron_entry_text
from activity.cron_entry import install_cron_entry


def test_the_entry_runs_the_recorder_every_minute_from_the_workspace_root() -> None:
    text = cron_entry_text(
        Path("/root/.local/bin/activity-record-memory"), Path("/home/user/workspace"), Path("/var/log/r.log")
    )
    schedule_line = text.splitlines()[-1]
    assert schedule_line == (
        "* * * * * root cd /home/user/workspace && /root/.local/bin/activity-record-memory >> /var/log/r.log 2>&1"
    )
    assert text.endswith("\n")


def test_the_entry_is_written_only_where_cron_reads_it_and_rewritten_when_it_changes(tmp_path: Path) -> None:
    system_dir = tmp_path / "etc" / "cron.d"
    install_cron_entry("* * * * * root true\n", system_dir)
    assert (system_dir / CRON_ENTRY_NAME).read_text() == "* * * * * root true\n"
    assert oct((system_dir / CRON_ENTRY_NAME).stat().st_mode & 0o777) == "0o644"
    assert [path.name for path in tmp_path.rglob("*") if path.is_file()] == [CRON_ENTRY_NAME]

    install_cron_entry("* * * * * root false\n", system_dir)
    assert (system_dir / CRON_ENTRY_NAME).read_text() == "* * * * * root false\n"


def test_an_unwritable_cron_directory_is_logged_not_raised(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")
    install_cron_entry("* * * * * root true\n", blocker)
    assert blocker.read_text() == ""
