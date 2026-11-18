"""Chat updates: a one-line note in a chat each time a usage limit goes up, such as "Claude usage 2%".

Off until the user turns it on from the page; the setting is one small JSON file beside the logs
(``settings.json`` in the data directory). When on, each new successful reading is compared with
the source's previous one, and every limit whose whole percent went up is named in one line. The
line goes to the chats on that account that were working between the two readings (the chats the
increase is credited to), through the chat app's loopback-only notes route; no chat working means
the usage happened elsewhere, and no line is sent. A note never reaches the AI in the chat, so it
cannot itself use more of the limit.
"""

import json
import os
import threading
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Final

from imbue.imbue_common.frozen_model import FrozenModel
from loguru import logger
from pydantic import Field, ValidationError

from ai_usage.provider import Limit

SETTINGS_FILENAME: Final[str] = "settings.json"
NOTE_SOURCE: Final[str] = "AI Usage Limits"
# Where the workspace's app registry is, and the chat app's address when it has no row there.
APPS_FILE_ENV: Final[str] = "MINDS_APPS_FILE"
DEFAULT_APPS_FILE: Final[str] = "data/.state/apps.toml"
CHAT_APP_NAME: Final[str] = "chat"
CHAT_APP_FALLBACK_URL: Final[str] = "http://127.0.0.1:8010"
POST_TIMEOUT_SECONDS: Final[float] = 5.0


class Settings(FrozenModel):
    """What the user chose on the page."""

    chat_updates: bool = Field(
        default=False,
        description="Whether a chat gets a one-line note each time a usage limit goes up",
    )


class SettingsStore:
    """The settings file, read on every use so an edit lands without a restart, and written whole."""

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / SETTINGS_FILENAME
        self._lock = threading.Lock()

    def read(self) -> Settings:
        try:
            return Settings.model_validate(json.loads(self.path.read_text()))
        except FileNotFoundError:
            return Settings()
        except (OSError, json.JSONDecodeError, ValidationError):
            logger.warning("Settings file {} is unreadable; using the defaults", self.path)
            return Settings()

    def write(self, settings: Settings) -> Settings:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(settings.model_dump_json(indent=2) + "\n")
            temporary.replace(self.path)
        return settings


def increase_text(label: str, before: list[Limit], after: list[Limit]) -> str | None:
    """The line for the limits whose whole percent went up between two readings, or None when none did.

    One limit: "Claude usage 2% (Current session)". Several: "Claude usage 2% (Current session), 15% (Weekly)".
    """
    previous = {limit.key: limit.percent for limit in before}
    parts = [
        f"{round(limit.percent)}% ({limit.label})"
        for limit in after
        if limit.key in previous and round(limit.percent) > round(previous[limit.key])
    ]
    if not parts:
        return None
    return f"{label} usage {', '.join(parts)}"


def chat_app_url(cwd: Path | None = None) -> str:
    """The chat app's base URL: its row in the app registry, else the port it has always used."""
    apps_file = Path(os.environ.get(APPS_FILE_ENV) or DEFAULT_APPS_FILE)
    if not apps_file.is_absolute():
        apps_file = (cwd or Path.cwd()) / apps_file
    try:
        rows: Any = tomllib.loads(apps_file.read_text()).get("apps", [])
    except (OSError, tomllib.TOMLDecodeError):
        return CHAT_APP_FALLBACK_URL
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and row.get("name") == CHAT_APP_NAME and isinstance(row.get("url"), str) and row["url"]:
            return row["url"].rstrip("/")
    return CHAT_APP_FALLBACK_URL


def post_note(chat_id: str, text: str) -> bool:
    """Put the line into one chat; whether the chat app took it. A failure is logged, never raised."""
    request = urllib.request.Request(
        f"{chat_app_url()}/api/chats/{chat_id}/app-notes",
        data=json.dumps({"source": NOTE_SOURCE, "text": text}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=POST_TIMEOUT_SECONDS) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        logger.warning("Could not put a usage note into chat {}: {}", chat_id, error)
        return False


def send_notes(chat_ids: list[str], text: str) -> None:
    """Put the line into each chat, off the caller's thread so a slow chat app never holds up a reading."""
    if not chat_ids:
        return

    def run() -> None:
        for chat_id in chat_ids:
            post_note(chat_id, text)

    threading.Thread(target=run, daemon=True, name="usage-chat-notes").start()
