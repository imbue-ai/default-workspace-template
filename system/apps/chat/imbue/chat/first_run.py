"""The first-run provider step: whether the workspace still owes its owner the "pick your AI" page.

Imbue Studio's first run ends inside the workspace, on a full-window page that asks which AI provider to
connect before the desktop is shown (specs/ftue-onboarding-v2 in mngr). A first-run seed marks the step
pending; the page's Skip or Next finishes it, and it is never shown again. The mark is one small file beside
the chat records, so it survives a restart of this app.
"""

import json
from pathlib import Path
from typing import Final

from loguru import logger

PROVIDER_STEP_FILENAME: Final[str] = "first_run_provider_step.json"
_IS_DONE_KEY: Final[str] = "is_done"


def _write(root: Path, is_done: bool) -> None:
    root.mkdir(parents=True, exist_ok=True)
    path = root / PROVIDER_STEP_FILENAME
    temp_path = path.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps({_IS_DONE_KEY: is_done}), encoding="utf-8")
    temp_path.replace(path)


def mark_provider_step_pending(root: Path) -> None:
    """Owe the page; a step already finished stays finished."""
    if not (root / PROVIDER_STEP_FILENAME).exists():
        _write(root, is_done=False)


def finish_provider_step(root: Path) -> None:
    _write(root, is_done=True)


def is_provider_step_pending(root: Path) -> bool:
    """Whether the page is still owed. An absent file is a workspace that never had a first run, and an
    unreadable one is treated as finished: the page must never come back on a workspace already in use."""
    path = root / PROVIDER_STEP_FILENAME
    if not path.is_file():
        return False
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.opt(exception=e).warning("Ignored an unreadable first-run file at {}", path)
        return False
    return isinstance(raw, dict) and raw.get(_IS_DONE_KEY) is False
