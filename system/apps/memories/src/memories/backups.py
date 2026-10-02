"""What the workspace's backups keep, so the page can say that a deleted note still sits in older snapshots.

The host-backup service snapshots the whole home tree, notes folder included, and keeps each snapshot until its
retention policy forgets it. Deleting a note therefore erases it from the workspace but not from the snapshots taken
before the delete. The retention is read through host-backup's own loader, so the page states what this workspace
actually runs with (the user can change it in ``data/system/backup.toml``) rather than a copy of its defaults.
"""

from datetime import timedelta
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from host_backup.config import BackupConfigError
from host_backup.config import RetentionSettings
from host_backup.config import load_backup_config
from host_backup.config import load_restic_env
from host_backup.config import missing_required_restic_keys
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

_DAYS_PER_MONTH: Final[float] = 30.44


class RetentionTier(FrozenModel):
    """One kept tier of snapshots: how often one is kept, and for how long."""

    cadence: str = Field(description="How often a snapshot is kept: hourly, daily, weekly or monthly")
    span_words: str = Field(description="How long the tier reaches back, in words")
    span: timedelta = Field(description="How long the tier reaches back")


class BackupRetention(FrozenModel):
    """Whether the workspace is backed up, and for how long a snapshot can outlive what it captured."""

    is_backed_up: bool = Field(description="Whether backups are set up for this workspace")
    longest_kept: str | None = Field(description="The longest a snapshot is kept, in words, or None when none are")
    schedule: tuple[str, ...] = Field(description="Each retention tier in words, shortest first")
    settings_path: str = Field(description="The file the retention is read from")


@pure
def _count_label(count: int, unit: str) -> str:
    return f"{count} {unit}" if count == 1 else f"{count} {unit}s"


@pure
def retention_tiers(retention: RetentionSettings) -> tuple[RetentionTier, ...]:
    """Each tier that keeps snapshots, shortest-cadence first."""
    candidates = (
        ("hourly", retention.keep_hourly, "hour", timedelta(hours=retention.keep_hourly)),
        ("daily", retention.keep_daily, "day", timedelta(days=retention.keep_daily)),
        ("weekly", retention.keep_weekly, "week", timedelta(weeks=retention.keep_weekly)),
        ("monthly", retention.keep_monthly, "month", timedelta(days=retention.keep_monthly * _DAYS_PER_MONTH)),
    )
    return tuple(
        RetentionTier(cadence=cadence, span_words=_count_label(count, unit), span=span)
        for cadence, count, unit, span in candidates
        if count > 0
    )


@pure
def describe_retention(retention: RetentionSettings, is_backed_up: bool, settings_path: Path) -> BackupRetention:
    tiers = retention_tiers(retention)
    longest = max(tiers, key=lambda tier: tier.span, default=None)
    return BackupRetention(
        is_backed_up=is_backed_up,
        longest_kept=longest.span_words if longest is not None else None,
        schedule=tuple(f"{tier.cadence} for {tier.span_words}" for tier in tiers),
        settings_path=str(settings_path),
    )


def read_backup_retention(backup_config_path: Path, restic_env_path: Path) -> BackupRetention:
    """The retention this workspace runs with, and whether backups are set up at all.

    An unreadable credentials file counts as backed up: the page then warns about a copy that may not exist, rather
    than promising that none does.
    """
    try:
        is_backed_up = not missing_required_restic_keys(load_restic_env(restic_env_path))
    except BackupConfigError as e:
        logger.debug("Could not read the backup credentials; assuming backups are on: {}", e)
        is_backed_up = True
    return describe_retention(load_backup_config(backup_config_path).retention, is_backed_up, backup_config_path)
