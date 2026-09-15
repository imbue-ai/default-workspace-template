"""Antigravity's model catalog and settings-backed resolver.

Antigravity reads its selected model from its per-agent ``settings.json``. The
setting uses the display name from ``agy models`` and includes the thinking tier
in parentheses, so the chat app exposes model families and writes the matching
display name when a model or effort changes.
"""

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from typing import Final

from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.harnesses.model import EffortChoice
from imbue.chat.harnesses.model import HarnessCatalog
from imbue.chat.harnesses.model import HarnessModelResolver
from imbue.chat.harnesses.model import ModelAxis
from imbue.chat.harnesses.model import ModelIdentity
from imbue.chat.harnesses.model import ModelOption
from imbue.chat.harnesses.model import PickerMode
from imbue.chat.harnesses.model import SwitchMode
from imbue.chat.harnesses.model import SwitchResult
from imbue.mngr.utils.file_utils import atomic_write
from imbue.mngr_antigravity.antigravity_config import get_antigravity_settings_path

_EFFORTS: Final[tuple[str, ...]] = ("low", "medium", "high")
_MODEL_ROWS: Final[tuple[tuple[str, str, tuple[str, ...]], ...]] = (
    ("gemini-3.8-flash", "Gemini 3.8 Flash", _EFFORTS),
    ("gemini-3.7-flash", "Gemini 3.7 Flash", _EFFORTS),
    ("gemini-3.6-flash", "Gemini 3.6 Flash", _EFFORTS),
    ("gemini-3.5-flash", "Gemini 3.5 Flash", _EFFORTS),
    ("gemini-3.1-pro", "Gemini 3.1 Pro", ("low", "high")),
    ("claude-sonnet-4-6", "Claude Sonnet 4.6 (Thinking)", ()),
    ("claude-opus-4-6-thinking", "Claude Opus 4.6 (Thinking)", ()),
    ("gpt-oss-120b-medium", "GPT-OSS 120B (Medium)", ()),
)

# Older state files may contain the launch slug with its tier baked in. Keep those
# rows matchable, but do not offer them as duplicate picker entries.
_LEGACY_MODEL_ROWS: Final[tuple[tuple[str, str, tuple[str, ...]], ...]] = tuple(
    (f"{model_id}-{effort}", label, efforts)
    for model_id, label, efforts in _MODEL_ROWS
    if efforts
    for effort in efforts
)

ANTIGRAVITY_HOME_RELATIVE_PATH: Final[Path] = Path("plugin") / "antigravity" / "home"
ANTIGRAVITY_STATE_RELATIVE_PATH: Final[Path] = Path(".")
_TIER_SUFFIX_RE: Final = re.compile(r" \((low|medium|high)\)$", re.IGNORECASE)
_VERSION_PAIR_RE: Final = re.compile(r"(?<=\d)-(?=\d)")
_TIER_SUFFIXES: Final[tuple[str, ...]] = ("high", "medium", "low")


def _option(model_id: str, label: str, efforts: tuple[str, ...], *, in_picker: bool = True) -> ModelOption:
    return ModelOption(
        id=model_id,
        label=label,
        efforts=tuple(EffortChoice(level=level) for level in efforts),
        supports_fast=False,
        in_picker=in_picker,
        harness_reported_model_id=label if in_picker else None,
    )


ANTIGRAVITY_CATALOG: Final[HarnessCatalog] = HarnessCatalog(
    options=tuple(_option(*row) for row in _MODEL_ROWS)
    + tuple(_option(*row, in_picker=False) for row in _LEGACY_MODEL_ROWS),
    switch_mode=SwitchMode.EAGER_THEN_RECONCILE,
    picker_mode=PickerMode.LIST,
    powered_by_text="Powered by Antigravity",
    native_atomic_shoulder_tap_possible=True,
)


def derived_option(model_id: str) -> ModelOption:
    """Return a readable, non-selectable fallback for a newly shipped model."""
    match = _TIER_SUFFIX_RE.search(model_id)
    if match:
        effort = match.group(1).lower()
        label = model_id[: match.start()]
    elif " " in model_id:
        effort = None
        label = model_id
    else:
        words = _VERSION_PAIR_RE.sub(".", model_id).split("-")
        effort = words.pop().lower() if len(words) > 1 and words[-1].lower() in _TIER_SUFFIXES else None
        label = " ".join(word.title() for word in words)
        if effort:
            label = f"{label} ({effort.title()})"
    return ModelOption(
        id=model_id,
        label=label,
        efforts=(EffortChoice(level=effort),) if effort else (),
        supports_fast=False,
        in_picker=False,
        harness_reported_model_id=model_id,
    )


class AntigravitySettingsError(ValueError):
    """The per-agent Antigravity settings file cannot be safely updated."""


def _read_settings_object(settings_path: Path) -> dict[str, Any]:
    try:
        raw = settings_path.read_text()
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise AntigravitySettingsError(f"Could not read Antigravity settings: {error}") from error
    try:
        settings = json.loads(raw)
    except json.JSONDecodeError as error:
        raise AntigravitySettingsError("Antigravity settings are not valid JSON") from error
    if not isinstance(settings, dict):
        raise AntigravitySettingsError("Antigravity settings are not a JSON object")
    return settings


def _display_name(identity: ModelIdentity) -> str | None:
    base_id = identity.model_id
    legacy = next((row for row in _LEGACY_MODEL_ROWS if row[0] == base_id), None)
    if legacy is not None:
        base_id = legacy[0].rsplit("-", 1)[0]
    row = next((row for row in _MODEL_ROWS if row[0] == base_id), None)
    if row is None:
        return None
    _, label, efforts = row
    if efforts:
        if identity.effort not in efforts:
            return None
        return f"{label} ({identity.effort.title()})"
    return label


class AntigravityModelResolver(HarnessModelResolver):
    """Apply model and effort changes through Antigravity's per-agent settings."""

    _settings_path: Path

    @classmethod
    def build(cls, agent_info: AgentInfo) -> "AntigravityModelResolver":
        self = cls.__new__(cls)
        agy_home = agent_info.agent_state_dir / ANTIGRAVITY_HOME_RELATIVE_PATH
        self._settings_path = get_antigravity_settings_path(agy_home)
        return self

    def switch(self, identity: ModelIdentity, axes: frozenset[ModelAxis], send: Callable[[str], bool]) -> SwitchResult:
        del send
        if ModelAxis.FAST in axes:
            return SwitchResult(ok=False, detail="Antigravity does not support fast mode")
        if not axes:
            return SwitchResult(ok=True)
        display_name = _display_name(identity)
        if display_name is None:
            return SwitchResult(ok=False, detail="That Antigravity model or effort is unavailable")
        try:
            settings = _read_settings_object(self._settings_path)
            settings["model"] = display_name
            atomic_write(self._settings_path, json.dumps(settings))
        except (AntigravitySettingsError, OSError) as error:
            detail = "Could not save Antigravity model settings"
            if isinstance(error, AntigravitySettingsError):
                detail = str(error)
            return SwitchResult(ok=False, detail=detail)
        return SwitchResult(ok=True)
