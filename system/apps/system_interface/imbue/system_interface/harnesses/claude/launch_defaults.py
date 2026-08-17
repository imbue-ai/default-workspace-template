"""The workspace's fast-mode launch default -- a launch-config concern, NOT part
of the per-agent model resolver.

A chat agent opens fast so the first exchange feels responsive; the opening prompt
then asks whether that speed is worth its higher per-token price, and the answer is
recorded here for the whole workspace. Every chat agent created afterward launches
with this setting, and no chat prompts again.

This is deliberately kept out of ``claude/model.py``: the resolver reads the
*agent's* settings files (which already encode whatever fast mode the agent launched
with), never this workspace-wide decision. The model resolver MUST NOT import this
module.
"""

import json
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.mngr.utils.file_utils import atomic_write

# Machine state, so it sits under data/.state/ next to apps.toml. JSON rather than
# TOML because nothing authors it by hand -- the system interface is the only
# writer, matching the workspace's other machine-written state.
_DECISION_RELATIVE_PATH: Final[str] = "data/.state/fast_mode_decision.json"

# The key both writers of the decision file agree on. Bootstrap parses the same
# file without importing this module (it must stay dependency-free), so the format
# is deliberately one boolean and nothing else.
_DECISION_KEY: Final[str] = "is_fast_mode_enabled"

# What a chat agent launches with before the workspace has answered the prompt.
# The opening conversation runs fast so it feels responsive; the prompt then asks
# whether that is worth its higher per-token price.
FAST_MODE_BEFORE_DECISION: Final[bool] = True


def get_workspace_fast_mode_decision_path(workspace_work_dir: Path) -> Path:
    return workspace_work_dir / _DECISION_RELATIVE_PATH


def read_workspace_fast_mode_decision(decision_path: Path) -> bool | None:
    """The workspace's recorded fast-mode answer, or None when it has not answered.

    Undecided is the file being absent, so there is no separate "decided" flag that
    could disagree with the value. A corrupt or wrong-shaped file also reads as
    undecided -- it must not strand the workspace at a setting nobody chose -- but
    unlike an absent one it is logged, since falling back turns on the setting that
    costs money.
    """
    try:
        raw = decision_path.read_text()
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.warning("Failed to read fast-mode decision at {}: {}", decision_path, e)
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("Ignored unparseable fast-mode decision at {}: {}", decision_path, e)
        return None
    is_enabled = data.get(_DECISION_KEY) if isinstance(data, dict) else None
    if not isinstance(is_enabled, bool):
        logger.warning("Ignored fast-mode decision at {} with no boolean {}: {}", decision_path, _DECISION_KEY, raw)
        return None
    return is_enabled


def write_workspace_fast_mode_decision(decision_path: Path, is_fast_mode_enabled: bool) -> None:
    """Record the workspace's answer, replacing any previous one."""
    decision_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(decision_path, json.dumps({_DECISION_KEY: is_fast_mode_enabled}))
