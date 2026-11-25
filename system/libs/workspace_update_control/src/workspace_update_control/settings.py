"""The update settings a workspace holds, and what the workspace can tell for itself about an update.

A client writes the settings (the shell's ``PUT /api/workspace/update/settings``): the release channel the
workspace follows, where that channel's file is read from, whether the workspace arms its own updates, and the
hours of the day an armed update may start in. They live in a file of their own, ``settings.json``, beside the
schedule: every client of the workspace sees the same ones, and the dispatcher reads them with no client present.

The hours are the owner's local ones. The workspace reads them in the timezone it records
(``data/.state/user_timezone``), and in UTC when it records none.

Like the rest of this library, everything here acts on the workspace whose root is the current directory.
"""

import os
import re
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Final, NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from workspace_update_control.dispatcher import (
    UPDATE_PROGRAM_SCHEMA,
    read_json_object,
    write_json_atomically,
    write_log_line,
)
from workspace_update_control.protocol import (
    BACKUP_CREDENTIALS_FILE_PATH,
    UPDATE_SETTINGS_FILE_PATH,
    USER_TIMEZONE_FILE_PATH,
)

# A channel is named in a URL (``<feed>/<channel>-web.json``), so it is held to what a file name's stem can be.
_CHANNEL_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_FEED_URL_PATTERN: Final[re.Pattern[str]] = re.compile(r"^https?://\S+$")
_LAST_HOUR_OF_THE_DAY: Final[int] = 23
_HOURS_PER_DAY: Final[int] = 24


class InvalidUpdateSettingsError(Exception):
    """The settings a client sent cannot be the workspace's update settings; the message says which and why."""


class UpdateSettings(NamedTuple):
    """The update settings a workspace holds."""

    # The release channel the workspace follows.
    channel: str
    # Where that channel's file is read from; '' on a tier that publishes none, where no version can be read.
    feed_base_url: str
    # Whether the account's workspaces arm their own updates.
    is_auto_update_enabled: bool
    # This workspace's own answer, which wins over the account's; None when it has none.
    is_auto_update_enabled_here: bool | None
    # The owner's local hours ``[start, end)`` an armed update may start in.
    window_start_hour: int
    window_end_hour: int

    @property
    def is_arming_enabled(self) -> bool:
        """Whether the workspace arms its own updates: its own answer when it has one, else the account's."""
        if self.is_auto_update_enabled_here is not None:
            return self.is_auto_update_enabled_here
        return self.is_auto_update_enabled

    def document(self) -> dict[str, object]:
        """The settings as a client reads and writes them."""
        return dict(self._asdict())


def _is_hour(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= _LAST_HOUR_OF_THE_DAY
    )


def _refusal(settings: UpdateSettings) -> str:
    """Why ``settings`` cannot be stored; '' when they can."""
    if _CHANNEL_PATTERN.match(settings.channel) is None:
        return "channel is a release channel's name: lower-case letters, digits and single dashes."
    if (
        settings.feed_base_url
        and _FEED_URL_PATTERN.match(settings.feed_base_url) is None
    ):
        return "feed_base_url is an http or https address, or empty where no feed is published."
    if not _is_hour(settings.window_start_hour) or not _is_hour(
        settings.window_end_hour
    ):
        return (
            "window_start_hour and window_end_hour are whole hours of the day, 0 to 23."
        )
    if settings.window_start_hour == settings.window_end_hour:
        return "window_start_hour and window_end_hour differ: a window of no length never opens."
    return ""


def write_update_settings(settings: UpdateSettings) -> UpdateSettings:
    """Store ``settings``, atomically, and answer them as stored; ``InvalidUpdateSettingsError`` when they cannot be."""
    refusal = _refusal(settings)
    if refusal:
        raise InvalidUpdateSettingsError(refusal)
    stored = read_update_settings()
    if stored != settings:
        write_json_atomically(
            UPDATE_SETTINGS_FILE_PATH,
            {"schema": UPDATE_PROGRAM_SCHEMA, **settings.document()},
        )
        write_log_line(
            "took update settings (channel %s, armed by the workspace: %s)"
            % (settings.channel, settings.is_arming_enabled)
        )
    return settings


def read_update_settings() -> UpdateSettings | None:
    """The stored settings; None when no client has written any, or the file does not hold usable ones."""
    stored = read_json_object(UPDATE_SETTINGS_FILE_PATH)
    if stored is None:
        return None
    channel = stored.get("channel")
    feed_base_url = stored.get("feed_base_url")
    is_enabled = stored.get("is_auto_update_enabled")
    is_enabled_here = stored.get("is_auto_update_enabled_here")
    start_hour = stored.get("window_start_hour")
    end_hour = stored.get("window_end_hour")
    if (
        not isinstance(channel, str)
        or not isinstance(feed_base_url, str)
        or not isinstance(is_enabled, bool)
        or not (is_enabled_here is None or isinstance(is_enabled_here, bool))
        or not isinstance(start_hour, int)
        or not isinstance(end_hour, int)
    ):
        write_log_line("the update settings file does not hold settings; reading none")
        return None
    settings = UpdateSettings(
        channel=channel,
        feed_base_url=feed_base_url,
        is_auto_update_enabled=is_enabled,
        is_auto_update_enabled_here=is_enabled_here,
        window_start_hour=start_hour,
        window_end_hour=end_hour,
    )
    refusal = _refusal(settings)
    if refusal:
        write_log_line("the update settings file holds unusable settings: %s" % refusal)
        return None
    return settings


def read_user_zone() -> tzinfo:
    """The timezone the workspace records for its user; UTC when it records none, or one this machine does not know."""
    try:
        with open(USER_TIMEZONE_FILE_PATH, encoding="utf-8") as handle:
            name = handle.read().strip()
    except (OSError, UnicodeDecodeError):
        return timezone.utc
    if not name:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return timezone.utc


def utc_update_window(
    start_hour: int, end_hour: int, zone: tzinfo, now: float
) -> tuple[int, int]:
    """The local ``[start_hour, end_hour)`` window in ``zone`` as ``(UTC start minute, length in minutes)``.

    The UTC start is taken from the window open at ``now``, else the next to open, so it holds until the zone's
    offset next changes. This is the conversion the Imbue Studio desktop makes for the values it sends
    (``utc_update_window`` in the mngr repository).
    """
    moment = datetime.fromtimestamp(now, timezone.utc)
    length = timedelta(hours=(end_hour - start_hour) % _HOURS_PER_DAY)
    local_today = moment.astimezone(zone).date()
    starts = (
        datetime(day.year, day.month, day.day, start_hour, tzinfo=zone)
        for day in (
            local_today - timedelta(days=1),
            local_today,
            local_today + timedelta(days=1),
        )
    )
    opened_at_utc = next(
        start for start in starts if moment < start + length
    ).astimezone(timezone.utc)
    return (
        opened_at_utc.hour * 60 + opened_at_utc.minute,
        int(length.total_seconds()) // 60,
    )


def settings_window(settings: UpdateSettings, now: float) -> tuple[int, int]:
    """The settings' window as a schedule holds one: ``(UTC start minute, length in minutes)``."""
    return utc_update_window(
        settings.window_start_hour, settings.window_end_hour, read_user_zone(), now
    )


def is_backup_configured() -> bool:
    """Whether the workspace holds backup credentials, and so has a restore point to fall back on."""
    return os.path.isfile(BACKUP_CREDENTIALS_FILE_PATH)
