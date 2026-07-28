import json
import os
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel

# How many complete user turns a chat agent runs with fast mode on before the
# workspace asks whether to keep it. This is the one knob for the grace period.
FAST_MODE_GRACE_TURN_COUNT: Final[int] = 5

# Machine state, so it sits under data/.state/ next to applications.toml. JSON
# rather than TOML because nothing authors it by hand -- the system interface is
# the only writer, matching the workspace's other machine-written state.
_DECISION_RELATIVE_PATH: Final[str] = "data/.state/fast_mode_decision.json"


class WorkspaceFastModeDecision(FrozenModel):
    """The workspace-wide answer to the fast-mode prompt, shared by every chat agent."""

    is_decided: bool = Field(description="Whether the user has answered the fast-mode prompt")
    is_fast_mode_enabled: bool = Field(description="The fast-mode setting chat agents launch with")


# Before the user answers, chat agents launch fast so the opening conversation feels
# responsive; the prompt then asks whether that is worth its higher per-token price.
UNDECIDED_FAST_MODE_DECISION: Final[WorkspaceFastModeDecision] = WorkspaceFastModeDecision(
    is_decided=False,
    is_fast_mode_enabled=True,
)


def get_workspace_fast_mode_decision_path(workspace_work_dir: Path) -> Path:
    return workspace_work_dir / _DECISION_RELATIVE_PATH


def read_workspace_fast_mode_decision(decision_path: Path) -> WorkspaceFastModeDecision:
    """Read the recorded decision, falling back to undecided when absent or unreadable.

    A corrupt file must not strand the workspace at whatever it last held, so this
    falls back to the undecided default and logs loudly enough to be noticed.
    """
    try:
        raw = decision_path.read_text()
    except FileNotFoundError:
        return UNDECIDED_FAST_MODE_DECISION
    except OSError as e:
        logger.warning("Failed to read fast-mode decision at {}: {}", decision_path, e)
        return UNDECIDED_FAST_MODE_DECISION
    try:
        return WorkspaceFastModeDecision.model_validate_json(raw)
    except ValidationError as e:
        logger.warning("Ignored malformed fast-mode decision at {}: {}", decision_path, e)
        return UNDECIDED_FAST_MODE_DECISION


def write_workspace_fast_mode_decision(decision_path: Path, decision: WorkspaceFastModeDecision) -> None:
    """Record the decision, replacing any previous one.

    Written through a temporary file in the same directory and renamed into place,
    so a chat create reading it concurrently sees either the old decision or the
    new one -- never a half-written file it would silently read as undecided.
    """
    decision_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = decision_path.with_suffix(f"{decision_path.suffix}.tmp")
    temporary_path.write_text(decision.model_dump_json())
    os.replace(temporary_path, decision_path)


def read_fast_mode_setting(settings_path: Path) -> bool | None:
    """The ``fastMode`` value in a Claude Code settings file, or None when it is not set.

    Absent and present-but-false are genuinely different here: Claude Code deletes
    the key when ``/fast`` turns fast mode off rather than writing false, so only a
    caller that knows the layering can decide what an absent key means.
    """
    try:
        raw = settings_path.read_text()
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("Ignored unparseable Claude settings at {}: {}", settings_path, e)
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("fastMode")
    return value if isinstance(value, bool) else None


def resolve_launch_fast_mode(claude_settings_path: Path, managed_settings_path: Path) -> bool:
    """Whether the agent's session started with fast mode on.

    Claude Code layers mngr's managed ``--settings`` file at command-line precedence,
    above the shared user settings, so a ``fastMode`` set there is what the session
    launched with. Only when the managed file leaves it unset does the user settings
    file decide, and an absent key there means off.
    """
    managed_setting = read_fast_mode_setting(managed_settings_path)
    if managed_setting is not None:
        return managed_setting
    user_setting = read_fast_mode_setting(claude_settings_path)
    if user_setting is not None:
        return user_setting
    return False
