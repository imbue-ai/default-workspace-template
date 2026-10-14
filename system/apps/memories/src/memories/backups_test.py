"""Tests for how long the backups keep a deleted note, read from the workspace's own backup settings."""

from pathlib import Path

from host_backup.config import RetentionSettings
from memories.backups import describe_retention
from memories.backups import read_backup_retention

_CONFIGURED_RESTIC_ENV = "RESTIC_REPOSITORY=s3:https://example.test/bucket\nRESTIC_PASSWORD=hunter2\n"


def test_the_default_retention_keeps_a_snapshot_for_up_to_24_months() -> None:
    retention = describe_retention(
        RetentionSettings(), is_backed_up=True, settings_path=Path("data/system/backup.toml")
    )

    assert retention.longest_kept == "24 months"
    assert retention.schedule == (
        "hourly for 24 hours",
        "daily for 30 days",
        "weekly for 12 weeks",
        "monthly for 24 months",
    )
    assert retention.settings_path == "data/system/backup.toml"


def test_the_longest_tier_is_chosen_by_span_and_empty_tiers_are_left_out() -> None:
    retention = describe_retention(
        RetentionSettings(keep_hourly=0, keep_daily=90, keep_weekly=2, keep_monthly=0),
        is_backed_up=True,
        settings_path=Path("backup.toml"),
    )

    assert retention.longest_kept == "90 days"
    assert retention.schedule == ("daily for 90 days", "weekly for 2 weeks")


def test_a_single_unit_is_not_pluralised_and_no_tiers_means_nothing_is_kept() -> None:
    assert (
        describe_retention(
            RetentionSettings(keep_hourly=0, keep_daily=0, keep_weekly=1, keep_monthly=0), True, Path("b.toml")
        ).longest_kept
        == "1 week"
    )
    nothing = describe_retention(
        RetentionSettings(keep_hourly=0, keep_daily=0, keep_weekly=0, keep_monthly=0), True, Path("b.toml")
    )
    assert nothing.longest_kept is None
    assert nothing.schedule == ()


def test_the_retention_comes_from_the_users_backup_settings_file(tmp_path: Path) -> None:
    settings = tmp_path / "backup.toml"
    settings.write_text("[retention]\nkeep_monthly = 6\n")
    restic_env = tmp_path / "restic.env"
    restic_env.write_text(_CONFIGURED_RESTIC_ENV)

    retention = read_backup_retention(settings, restic_env)

    assert retention.is_backed_up is True
    assert retention.longest_kept == "6 months"


def test_a_workspace_without_backup_credentials_is_not_backed_up(tmp_path: Path) -> None:
    missing = read_backup_retention(tmp_path / "backup.toml", tmp_path / "restic.env")
    assert missing.is_backed_up is False

    incomplete_env = tmp_path / "incomplete.env"
    incomplete_env.write_text("RESTIC_REPOSITORY=s3:https://example.test/bucket\n")
    assert read_backup_retention(tmp_path / "backup.toml", incomplete_env).is_backed_up is False


def test_unreadable_credentials_count_as_backed_up_so_the_page_never_promises_no_copy(tmp_path: Path) -> None:
    unreadable_env = tmp_path / "restic.env"
    unreadable_env.mkdir()

    assert read_backup_retention(tmp_path / "backup.toml", unreadable_env).is_backed_up is True
