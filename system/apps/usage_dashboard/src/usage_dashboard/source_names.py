"""The names the page shows a chat's or a routine's usage under, in place of their ids.

A chat's title is the chat app's: this server asks it for its chat list at the URL the app registry gives it. A
routine's title is in its definition file, read with the routines library's own parser. Either can be missing
(the chat app is down, a definition is broken or gone), and the page then shows the id, so a lookup that fails
answers what it found and logs why.
"""

import tomllib
from pathlib import Path
from typing import Final

import httpx
from loguru import logger
from pydantic import ValidationError

from app_manifest.registry import RegistryReadError
from app_manifest.registry import read_registry
from routines.definitions import parse_definition
from routines.paths import DEFINITION_SUFFIX

CHAT_APP_NAME: Final[str] = "chat"
CHAT_LIST_PATH: Final[str] = "/api/chats"
_CHAT_LIST_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(3.0)


def chat_titles(apps_file: Path, http: httpx.Client) -> dict[str, str]:
    """Each chat's title by its id, from the chat app; empty when the chat app cannot be found or asked."""
    try:
        rows = read_registry(apps_file)
    except RegistryReadError as e:
        logger.warning("Could not read the app registry to find the chat app: {}", e)
        return {}
    chat_app = next((row for row in rows if str(row.name) == CHAT_APP_NAME), None)
    if chat_app is None:
        return {}
    try:
        response = http.get(f"{str(chat_app.url).rstrip('/')}{CHAT_LIST_PATH}", timeout=_CHAT_LIST_TIMEOUT)
        response.raise_for_status()
        chats = response.json()["chats"]
        return {str(chat["chat_id"]): str(chat["title"]) for chat in chats}
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
        logger.warning("Could not read the chat titles from the chat app: {}", e)
        return {}


def routine_titles(definitions_dir: Path) -> dict[str, str]:
    """Each routine's title by its id (its definition file's stem); a file that does not read is left out."""
    if not definitions_dir.is_dir():
        return {}
    titles: dict[str, str] = {}
    for path in sorted(definitions_dir.glob(f"*{DEFINITION_SUFFIX}")):
        try:
            titles[path.stem] = str(parse_definition(path.read_text()).title)
        except (OSError, tomllib.TOMLDecodeError, ValidationError) as e:
            logger.warning("Could not read the routine title in {}: {}", path.name, e)
    return titles
