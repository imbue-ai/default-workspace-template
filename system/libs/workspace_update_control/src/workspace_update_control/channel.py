"""The version a workspace's release channel is on: the newest version the workspace may take.

The release feed publishes one file for each channel, ``<feed>/<channel>-web.json``, that names a template
version (``templateRef``). A new workspace on that channel is created at it, and a workspace on that channel
updates to it. The workspace reads the file itself, at most once an hour, and keeps the last answer in
``channel-version.json`` with how the last read went, so a client shows what is available without reading the
feed, and a read that fails leaves the last version on display.

A client from before the workspace read the feed names the newest version it knows in every request
(``ceiling``). While such a client keeps asking, its version bounds the channel's: that client's own build is what
it was verified against.
"""

import http.client
import json
import re
import threading
import urllib.request
from collections.abc import Callable, Mapping
from typing import Final

from workspace_update_control.dispatcher import (
    UPDATE_PROGRAM_SCHEMA,
    read_json_object,
    write_json_atomically,
    write_log_line,
)
from workspace_update_control.installed_version import release_sort_key
from workspace_update_control.protocol import (
    CHANNEL_VERSION_FILE_PATH,
    CLIENT_CEILING_FILE_PATH,
)
from workspace_update_control.settings import UpdateSettings

# The text of the channel's file at an address; ``ChannelFileUnreadableError`` when it cannot be had.
ChannelFileFetcher = Callable[[str], str]

CHANNEL_READ_INTERVAL_SECONDS: Final[float] = 60 * 60
# A desktop from before the workspace armed its own updates sweeps every 300 seconds, naming its newest version
# each time. Its word stands for three sweeps after the last, so one missed sweep does not let the workspace
# arm past it.
CLIENT_CEILING_HOLD_SECONDS: Final[float] = 15 * 60

# The key the channel's file names the version under, as the release feed's publisher writes it
# (``TEMPLATE_REF_KEY`` in the mngr repository's ``scripts/release_channel/web_channels.py``).
_TEMPLATE_REF_KEY: Final[str] = "templateRef"
_TEMPLATE_TAG_PATTERN: Final[re.Pattern[str]] = re.compile(r"^minds-v\d+\.\d+\.\d+$")
_FETCH_TIMEOUT_SECONDS: Final[float] = 10.0
_MAX_CHANNEL_FILE_BYTES: Final[int] = 64 * 1024
# The feed's CDN refuses ``Python-urllib/<version>`` by name.
_USER_AGENT: Final[str] = "imbue-workspace"
_ERROR_MAX_CHARS: Final[int] = 300


class ChannelFileUnreadableError(Exception):
    """The channel's file could not be read, or does not name a release; the message says why."""


def channel_file_url(feed_base_url: str, channel: str) -> str:
    return "%s/%s-web.json" % (feed_base_url.rstrip("/"), channel)


def parse_channel_file(text: str) -> str:
    """The release the channel's file names; ``ChannelFileUnreadableError`` for a file that names none."""
    try:
        document: object = json.loads(text)
    except ValueError as exc:
        raise ChannelFileUnreadableError("the channel's file is not JSON") from exc
    template_ref = (
        document.get(_TEMPLATE_REF_KEY) if isinstance(document, dict) else None
    )
    if (
        not isinstance(template_ref, str)
        or _TEMPLATE_TAG_PATTERN.match(template_ref.strip()) is None
    ):
        raise ChannelFileUnreadableError(
            "the channel's file names no minds-vX.Y.Z release under %s"
            % _TEMPLATE_REF_KEY
        )
    return template_ref.strip()


def _read_address(url: str, texts: list[str], failures: list[str]) -> None:
    if not url.startswith(("https://", "http://")):
        failures.append("%s is not an http address" % url)
        return
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(
            request, timeout=_FETCH_TIMEOUT_SECONDS
        ) as response:
            body = response.read(_MAX_CHANNEL_FILE_BYTES + 1)
    except (OSError, http.client.HTTPException, ValueError) as exc:
        failures.append(str(exc) or type(exc).__name__)
        return
    if len(body) > _MAX_CHANNEL_FILE_BYTES:
        failures.append("the channel's file is larger than any channel's file")
        return
    try:
        texts.append(body.decode("utf-8"))
    except UnicodeDecodeError as exc:
        failures.append(str(exc))


def fetch_channel_file(url: str) -> str:
    """Read the channel's file at ``url`` off the feed, within ``_FETCH_TIMEOUT_SECONDS`` however the feed behaves.

    The read runs on a thread of its own and is given up on when the time is out: a socket's own timeout bounds
    one wait on it, not a name lookup that hangs or a feed that sends a byte at a time.
    """
    texts: list[str] = []
    failures: list[str] = []
    reader = threading.Thread(
        target=_read_address,
        args=(url, texts, failures),
        daemon=True,
        name="channel-file-read",
    )
    reader.start()
    reader.join(_FETCH_TIMEOUT_SECONDS)
    if texts:
        return texts[0]
    raise ChannelFileUnreadableError(
        failures[0]
        if failures
        else "%s did not answer in %d seconds" % (url, _FETCH_TIMEOUT_SECONDS)
    )


def _is_for(state: Mapping[str, object], settings: UpdateSettings) -> bool:
    return (
        state.get("channel") == settings.channel
        and state.get("feed_base_url") == settings.feed_base_url
    )


def read_channel_version(settings: UpdateSettings | None) -> dict[str, object] | None:
    """What the workspace last read for the channel ``settings`` name: ``channel``, ``feed_base_url``,
    ``template_ref`` ('' when no read has succeeded), ``read_at`` (when that version was read), ``checked_at``
    (the last read, successful or not) and ``error`` ('' when the last read succeeded). None when it has read
    nothing for that channel."""
    if settings is None:
        return None
    state = read_json_object(CHANNEL_VERSION_FILE_PATH)
    if state is None or not _is_for(state, settings):
        return None
    return state


def channel_template_ref(state: Mapping[str, object] | None) -> str:
    """The version ``state`` holds; '' when it holds none."""
    template_ref = state.get("template_ref") if state is not None else None
    return template_ref if isinstance(template_ref, str) else ""


def is_channel_version_current(state: Mapping[str, object] | None) -> bool:
    """Whether ``state`` holds a version and the last read of the channel's file succeeded."""
    return (
        state is not None
        and bool(channel_template_ref(state))
        and not state.get("error")
    )


def _is_read_due(
    state: Mapping[str, object] | None, now: float, is_due_when_unread: bool
) -> bool:
    if state is None:
        return True
    if is_due_when_unread and not channel_template_ref(state):
        return True
    checked_at = state.get("checked_at")
    if isinstance(checked_at, bool) or not isinstance(checked_at, (int, float)):
        return True
    # A clock that went back must not put the next read off for good.
    return not 0 <= now - checked_at < CHANNEL_READ_INTERVAL_SECONDS


def refresh_channel_version(
    settings: UpdateSettings,
    now: float,
    fetch: ChannelFileFetcher,
    is_due_when_unread: bool,
) -> dict[str, object] | None:
    """The channel's version, read off the feed when the last read is an hour old (or, with
    ``is_due_when_unread``, when no read has succeeded yet), and otherwise as last read.

    A read that fails keeps the version the last one read and records why it failed. None where the settings
    name no feed, which is a tier that publishes no channel files.
    """
    if not settings.feed_base_url:
        return None
    state = read_channel_version(settings)
    if not _is_read_due(state, now, is_due_when_unread):
        return state
    refreshed: dict[str, object] = {
        "schema": UPDATE_PROGRAM_SCHEMA,
        "channel": settings.channel,
        "feed_base_url": settings.feed_base_url,
        "template_ref": channel_template_ref(state),
        "read_at": state.get("read_at") if state is not None else None,
        "checked_at": now,
        "error": "",
    }
    url = channel_file_url(settings.feed_base_url, settings.channel)
    try:
        refreshed["template_ref"] = parse_channel_file(fetch(url))
        refreshed["read_at"] = now
    except ChannelFileUnreadableError as exc:
        refreshed["error"] = str(exc)[:_ERROR_MAX_CHARS]
        write_log_line("could not read the channel's version from %s: %s" % (url, exc))
    if state is None or channel_template_ref(state) != refreshed["template_ref"]:
        write_log_line(
            "the %s channel is on %s"
            % (
                settings.channel,
                refreshed["template_ref"] or "no version this workspace could read",
            )
        )
    write_json_atomically(CHANNEL_VERSION_FILE_PATH, refreshed)
    return refreshed


def record_client_ceiling(ceiling: str, now: float) -> None:
    """Record that a client named ``ceiling`` as the newest version it knows, at ``now``."""
    stored = read_json_object(CLIENT_CEILING_FILE_PATH)
    if (
        stored is not None
        and stored.get("ceiling") == ceiling
        and stored.get("asserted_at") == now
    ):
        return
    write_json_atomically(
        CLIENT_CEILING_FILE_PATH,
        {"schema": UPDATE_PROGRAM_SCHEMA, "ceiling": ceiling, "asserted_at": now},
    )


def read_fresh_client_ceiling(now: float) -> str | None:
    """The newest version a client named within ``CLIENT_CEILING_HOLD_SECONDS``; None when none did."""
    stored = read_json_object(CLIENT_CEILING_FILE_PATH)
    if stored is None:
        return None
    ceiling = stored.get("ceiling")
    asserted_at = stored.get("asserted_at")
    if not isinstance(ceiling, str) or not ceiling:
        return None
    if isinstance(asserted_at, bool) or not isinstance(asserted_at, (int, float)):
        return None
    return ceiling if 0 <= now - asserted_at < CLIENT_CEILING_HOLD_SECONDS else None


def bounded_by_client_ceiling(
    channel_ref: str, client_ceiling: str | None
) -> str | None:
    """The older of the channel's version and the version a client still asking named; the channel's when no
    client is. None when the client's names no release, so nothing says which of the two is older."""
    if client_ceiling is None:
        return channel_ref
    channel_key = release_sort_key(channel_ref)
    client_key = release_sort_key(client_ceiling)
    if channel_key is None or client_key is None:
        return None
    return channel_ref if channel_key <= client_key else client_ceiling
