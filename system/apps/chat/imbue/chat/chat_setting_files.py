"""A setting that belongs to one chat, kept as one small JSON file in the chat's own folder.

Such a setting travels with the chat across handoffs and rebinds and survives restarts, since the
folder is the chat's rather than any one agent's. ``chat_fast_mode.py`` and ``chat_autocompact.py``
each keep one this way.
"""

import json
from pathlib import Path
from typing import TypeVar

from loguru import logger as _loguru_logger
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel

logger = _loguru_logger

_SettingT = TypeVar("_SettingT", bound=FrozenModel)


def read_chat_setting_file(
    chat_dir: Path, filename: str, model_type: type[_SettingT], description: str
) -> _SettingT | None:
    """The chat's setting as written; None for a chat that has none yet, or an unreadable file (warned about).

    ``description`` names the setting in the warning, e.g. ``"fast mode"``.
    """
    path = chat_dir / filename
    if not path.is_file():
        return None
    try:
        return model_type.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, ValidationError) as e:
        logger.warning("Ignoring an unreadable {} file at {}: {}", description, path, e)
        return None


def write_chat_setting_file(chat_dir: Path, filename: str, state: FrozenModel) -> None:
    """Write the chat's setting whole (a temp file renamed into place)."""
    chat_dir.mkdir(parents=True, exist_ok=True)
    path = chat_dir / filename
    temp_path = path.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(state.model_dump(mode="json"), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp_path.replace(path)
