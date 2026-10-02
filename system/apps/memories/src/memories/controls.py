"""The user's memory switches: pause memory for every chat, or turn it off for one kind of chat.

They are kept in ``data/.apps/memories/settings.json``, which ``system/scripts/agent_memory_context.py`` reads before
every pi and Claude message; no file means memory is on for everyone. Claude Code also reads
``autoMemoryEnabled`` from the workspace's ``.claude/settings.local.json`` when a chat starts, and a new Claude chat
with it false neither loads nor saves memory, so saving the switches also sets that key (and removes it when Claude's
memory is back on), leaving every other key in that file as it was.
"""

import json
from enum import auto
from pathlib import Path
from typing import Any
from typing import Final

from pydantic import ConfigDict
from pydantic import Field
from pydantic import ValidationError

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from memories.errors import ControlsReadError
from memories.errors import NoteWriteError
from memories.notes import write_atomically

CLAUDE_AUTO_MEMORY_KEY: Final[str] = "autoMemoryEnabled"


class MemoryHarness(UpperCaseStrEnum):
    """A kind of chat that uses the shared notes."""

    CLAUDE = auto()
    PI_CODING = auto()


class MemoryControls(FrozenModel):
    """The switches as saved: a missing file reads as the defaults, memory on for every chat."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    is_paused: bool = Field(default=False, description="Whether memory is off for every chat")
    disabled_harnesses: tuple[MemoryHarness, ...] = Field(
        default=(), description="The kinds of chat memory is off for while it is not paused"
    )


@pure
def is_memory_on_for(controls: MemoryControls, harness: MemoryHarness) -> bool:
    return not controls.is_paused and harness not in controls.disabled_harnesses


def read_controls(controls_path: Path) -> MemoryControls:
    try:
        text = controls_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return MemoryControls()
    except OSError as e:
        raise ControlsReadError(f"could not read {controls_path}: {e}") from e
    try:
        return MemoryControls.model_validate_json(text)
    except ValidationError as e:
        raise ControlsReadError(f"{controls_path} is not valid memory settings: {e.errors()[0]['msg']}") from e


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise ControlsReadError(f"could not read {path}: {e}") from e
    try:
        parsed = json.loads(text)
    except ValueError as e:
        raise ControlsReadError(f"{path} is not valid JSON, so Claude's memory switch was not changed: {e}") from e
    if not isinstance(parsed, dict):
        raise ControlsReadError(f"{path} is not a JSON object, so Claude's memory switch was not changed")
    return parsed


@pure
def with_claude_auto_memory(settings: dict[str, Any], is_on: bool) -> dict[str, Any]:
    """``settings`` with Claude's memory turned off, or with the app's key removed so Claude's default (on) applies."""
    kept = {key: value for key, value in settings.items() if key != CLAUDE_AUTO_MEMORY_KEY}
    return kept if is_on else {**kept, CLAUDE_AUTO_MEMORY_KEY: False}


def save_controls(controls: MemoryControls, controls_path: Path, claude_settings_path: Path) -> None:
    """Save the switches, then point new Claude chats at them. The caller serializes calls.

    Claude's settings file is read first, so one that cannot be changed refuses the save before anything is written.
    """
    claude_settings = _read_json_object(claude_settings_path)
    updated = with_claude_auto_memory(claude_settings, is_memory_on_for(controls, MemoryHarness.CLAUDE))
    try:
        controls_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise NoteWriteError(f"could not create {controls_path.parent}: {e}") from e
    write_atomically(controls_path, controls.model_dump_json(indent=2) + "\n")
    if updated == claude_settings:
        return
    try:
        claude_settings_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise NoteWriteError(f"could not create {claude_settings_path.parent}: {e}") from e
    write_atomically(claude_settings_path, json.dumps(updated, indent=2) + "\n")
