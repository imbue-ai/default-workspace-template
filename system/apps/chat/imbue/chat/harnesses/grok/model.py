"""grok's model catalog: grok 1.0.62's built-in model set, as its binary embeds it, each model with the efforts it
declares."""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from typing import Final

from imbue.chat.harnesses.model import EffortChoice
from imbue.chat.harnesses.model import HarnessCatalog
from imbue.chat.harnesses.model import ModelOption
from imbue.chat.harnesses.model import PickerMode
from imbue.chat.harnesses.model import SwitchMode
from imbue.imbue_common.pure import pure

# `default_models.json` as the grok 1.0.62 binary embeds it.
BINARY_DEFAULT_MODELS_PATH: Final[Path] = Path(__file__).parent / "binary_default_models_v1_0_62.json"


@pure
def build_catalog_options(default_models: Mapping[str, Any]) -> tuple[ModelOption, ...]:
    """One option per model in grok's default-models document, with the efforts it declares, lowest first."""
    return tuple(
        ModelOption(
            id=model["id"],
            label=model["name"],
            # grok declares a model's efforts highest first.
            efforts=tuple(
                EffortChoice(level=effort["value"]) for effort in reversed(model.get("reasoning_efforts", ()))
            ),
            default_effort=model.get("reasoning_effort"),
            supports_fast=False,
            harness_reported_model_id=model["model"] if model["model"] != model["id"] else None,
        )
        for model in default_models["models"]
    )


GROK_CATALOG: Final[HarnessCatalog] = HarnessCatalog(
    options=build_catalog_options(json.loads(BINARY_DEFAULT_MODELS_PATH.read_text())),
    switch_mode=SwitchMode.EAGER_THEN_RECONCILE,
    picker_mode=PickerMode.LIST,
    powered_by_text="Powered by Grok Build",
    # The tap cancels the turn with one Ctrl+C and the flush worker types the held messages; nothing restarts.
    native_atomic_shoulder_tap_possible=True,
)
