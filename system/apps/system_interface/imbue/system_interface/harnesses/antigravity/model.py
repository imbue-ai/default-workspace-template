"""antigravity's model catalog and its READ-ONLY model resolver.

Empirically confirmed against a live agy (1.1.10/1.1.11): agy stores the agent's model
in its per-agent ``antigravity-cli/settings.json`` ``"model"`` key -- a DISPLAY name like
``"Gemini 3.6 Flash (High)"`` -- which it READS at launch and REWRITES on every in-agy
``/model`` change. So the resolver reads that one key for both the launch guess and the live
selection: persisted (restart-robust), updated on change, and it is the file mngr already
provisions (the launcher pins ``settings_overrides.model`` there). No transcript, no state
file, no display->slug mapping.

The bar is READ-ONLY: agy's ``/model`` is an interactive picker with no scriptable one-shot
form, so ``switch`` sends nothing. Effort is NOT a separate axis here -- ``agy models``
enumerates every model+effort combination as its own entry, so each catalog option is one
whole display string with an empty effort set. The bar reflects an in-agy ``/model`` change
(``watched_paths`` covers settings.json) but cannot drive one.
"""

from collections.abc import Callable
from pathlib import Path

from imbue.mngr.utils.file_utils import read_json_dict
from imbue.mngr_antigravity.antigravity_config import get_antigravity_settings_path
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.model import HarnessCatalog
from imbue.system_interface.harnesses.model import HarnessModelResolver
from imbue.system_interface.harnesses.model import ModelAxis
from imbue.system_interface.harnesses.model import ModelIdentity
from imbue.system_interface.harnesses.model import ModelOption
from imbue.system_interface.harnesses.model import PickerMode
from imbue.system_interface.harnesses.model import SwitchMode
from imbue.system_interface.harnesses.model import SwitchResult

_ICON: str = (Path(__file__).parent / "icon.svg").read_text()

# The per-agent agy ``$HOME`` relative to the agent state dir (== mngr_antigravity's
# _AGY_HOME_RELATIVE_PATH). agy's settings.json lives under this home's ``.gemini`` tree.
# Kept local, mirroring codex_session_parser's reimplement-don't-import-a-private stance.
_AGY_HOME_RELATIVE: tuple[str, ...] = ("plugin", "antigravity", "home")

# The catalog, verbatim from ``agy models`` (display names). Each model+effort pairing is its
# own entry, so there is no effort axis -- ``efforts`` is empty for every option and the bar
# shows only the model slot. The id is the display string agy writes to settings.json's
# ``model`` key, so a live read matches an option with no translation.
_MODEL_LABELS: tuple[str, ...] = (
    "Gemini 3.6 Flash (High)",
    "Gemini 3.6 Flash (Medium)",
    "Gemini 3.6 Flash (Low)",
    "Gemini 3.5 Flash (High)",
    "Gemini 3.5 Flash (Medium)",
    "Gemini 3.5 Flash (Low)",
    "Gemini 3.1 Pro (High)",
    "Gemini 3.1 Pro (Low)",
    "Claude Sonnet 4.6 (Thinking)",
    "Claude Opus 4.6 (Thinking)",
    "GPT-OSS 120B (Medium)",
)

# The account default (what a fresh agy launches on, and the launcher's pin).
_DEFAULT_MODEL: str = "Gemini 3.6 Flash (High)"

ANTIGRAVITY_CATALOG: HarnessCatalog = HarnessCatalog(
    options=tuple(
        ModelOption(id=label, label=label, efforts=(), supports_fast=False) for label in _MODEL_LABELS
    ),
    default_model_id=_DEFAULT_MODEL,
    # READ_ONLY: agy exposes no scriptable model switch, so the slots display but are inert.
    switch_mode=SwitchMode.READ_ONLY,
    # A small hand-written set -- a plain list, not a search box.
    picker_mode=PickerMode.LIST,
    icon_svg=_ICON,
)


class AntigravityModelResolver(HarnessModelResolver):
    """Reads an antigravity agent's model from its own ``settings.json`` ``model`` key.
    Display-only (READ_ONLY): agy has no scriptable model switch."""

    _settings_path: Path

    @classmethod
    def build(cls, agent_info: AgentInfo) -> "AntigravityModelResolver":
        self = cls.__new__(cls)
        self._settings_path = get_antigravity_settings_path(agent_info.agent_state_dir.joinpath(*_AGY_HOME_RELATIVE))
        return self

    def _read_settings_model(self) -> ModelIdentity | None:
        """The ``model`` display string from the per-agent settings.json, or None.

        agy stores a display name (``"Gemini 3.6 Flash (High)"``); effort is part of that label,
        not a separate axis, so ``effort`` is always None. ``read_json_dict`` is the shared
        safe reader (missing / malformed / non-object settings -> ``{}``), so a mid-write
        settings.json or a missing/blank ``model`` yields None rather than raising."""
        model = read_json_dict(self._settings_path).get("model")
        if not isinstance(model, str) or not model:
            return None
        return ModelIdentity(model_id=model, effort=None, fast=False)

    def guess_from_launch(self) -> ModelIdentity | None:
        # The launcher pins ``settings_overrides.model``, so the per-agent settings.json carries
        # the model from provision -- read it as the pre-turn guess.
        return self._read_settings_model()

    def read_live(self) -> ModelIdentity | None:
        # agy rewrites settings.json's ``model`` on an in-agy ``/model`` change, so re-reading it
        # is the live selection -- persisted and restart-robust, not a transcript/event read.
        return self._read_settings_model()

    def watched_paths(self) -> tuple[Path, ...]:
        # agy's write to settings.json on ``/model`` re-fires the recompute so the bar updates.
        return (self._settings_path,)

    def switch(
        self, identity: ModelIdentity, axes: frozenset[ModelAxis], send: Callable[[str], bool]
    ) -> SwitchResult:
        # READ_ONLY: agy's ``/model`` is an interactive picker, so there is nothing to send. To
        # change the model, re-pin ``settings_overrides.model`` on the agent type and recreate,
        # or use agy's own ``/model`` in the terminal (which the bar then reflects).
        return SwitchResult(ok=False, detail="Antigravity model switching is not available from the bar")
