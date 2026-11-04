"""The app registry (``data/.state/apps.toml``), as far as the library reads it: where the chat app is, and what
each app is called."""

from collections.abc import Mapping
from pathlib import Path
from typing import Final

from loguru import logger

from app_manifest.errors import RegistryReadError
from app_manifest.registry import read_registry
from app_manifest.registry_location import DEFAULT_APPS_FILE
from app_manifest.registry_location import ENV_APPS_FILE

CHAT_APP_NAME: Final[str] = "chat"
# The port the chat app has always used, for a workspace whose registry does not list it yet.
CHAT_APP_FALLBACK_URL: Final[str] = "http://127.0.0.1:8010"


def registry_path_of(repo_root: Path, environ: Mapping[str, str]) -> Path:
    """The registry's location, as ``forward_port.py`` resolves it: the environment's, else the default under the root."""
    named = environ.get(ENV_APPS_FILE, "")
    return Path(named) if named else repo_root / DEFAULT_APPS_FILE


def chat_app_url(registry_path: Path) -> str:
    """The chat app's address from its registry row, else its fixed port."""
    try:
        rows = read_registry(registry_path)
    except RegistryReadError as e:
        logger.warning("Using the chat app's fixed address: {}", e)
        return CHAT_APP_FALLBACK_URL
    for row in rows:
        if row.name == CHAT_APP_NAME:
            return str(row.url).rstrip("/")
    return CHAT_APP_FALLBACK_URL


def app_display_names(registry_path: Path) -> dict[str, str]:
    """What each registered app is called, by name; an app with no display name is left out."""
    try:
        rows = read_registry(registry_path)
    except RegistryReadError as e:
        logger.warning("Grouping routines by app name alone: {}", e)
        return {}
    return {str(row.name): str(row.display_name) for row in rows if row.display_name is not None}
