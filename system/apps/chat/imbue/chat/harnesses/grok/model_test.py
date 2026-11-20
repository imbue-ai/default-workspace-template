"""Unit tests for grok's model catalog."""

import json
from pathlib import Path
from typing import Any

from imbue.chat.harnesses.grok.model import BINARY_DEFAULT_MODELS_PATH
from imbue.chat.harnesses.grok.model import GROK_CATALOG
from imbue.chat.harnesses.grok.model import build_catalog_options
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.model import ModelIdentity
from imbue.chat.harnesses.model import PickerMode
from imbue.chat.harnesses.model import SwitchMode
from imbue.chat.harnesses.model import match_option
from imbue.chat.harnesses.model import read_model_identity
from imbue.chat.harnesses.model import resolve_model_choice
from imbue.chat.harnesses.model import validate_model_pick
from imbue.chat.harnesses.registry import get_catalog
from imbue.chat.harnesses.registry import get_model_state_path
from imbue.mngr_grok.resources.grok_statusline import MODEL_STATE_FILENAME


def _binary_models() -> list[dict[str, Any]]:
    return json.loads(BINARY_DEFAULT_MODELS_PATH.read_text())["models"]


def test_the_catalog_is_grok_1_0_62_s_built_in_model_set() -> None:
    assert [(option.id, option.label) for option in GROK_CATALOG.options] == [
        (model["id"], model["name"]) for model in _binary_models()
    ]
    for option, model in zip(GROK_CATALOG.options, _binary_models(), strict=True):
        assert {choice.level for choice in option.efforts} == {
            effort["value"] for effort in model["reasoning_efforts"]
        }
        assert [effort["value"] for effort in model["reasoning_efforts"] if effort.get("default")] == [
            option.default_effort
        ]
        assert option.supports_fast is False
        assert option.in_picker is True
        assert option.harness_reported_model_id is None


def test_the_catalog_offers_each_models_efforts_lowest_first() -> None:
    assert [(option.id, tuple(choice.level for choice in option.efforts)) for option in GROK_CATALOG.options] == [
        ("grok-4.6", ("low", "medium", "high", "xhigh")),
        ("grok-4.5", ("low", "medium", "high")),
    ]


def test_a_model_whose_api_id_differs_reports_that_id_and_one_without_efforts_has_no_effort_axis() -> None:
    (option,) = build_catalog_options({"models": [{"id": "fast-alias", "model": "grok-fast-2", "name": "Grok Fast"}]})

    assert option.id == "fast-alias"
    assert option.harness_reported_model_id == "grok-fast-2"
    assert option.efforts == ()
    assert option.default_effort is None
    assert match_option(ModelIdentity(model_id="grok-fast-2", effort=None, fast=False), (option,)) == option


def test_the_registry_serves_the_catalog_and_switches_eagerly_from_a_list() -> None:
    assert get_catalog(HarnessType.GROK) == GROK_CATALOG
    assert GROK_CATALOG.switch_mode is SwitchMode.EAGER_THEN_RECONCILE
    assert GROK_CATALOG.picker_mode is PickerMode.LIST
    assert GROK_CATALOG.powered_by_text == "Powered by Grok Build"


def test_every_model_matches_a_live_read_at_each_of_its_efforts_and_no_other() -> None:
    for option in GROK_CATALOG.options:
        for choice in option.efforts:
            identity = ModelIdentity(model_id=option.id, effort=choice.level, fast=False)
            assert match_option(identity, GROK_CATALOG.options) == option
    grok_4_5_at_xhigh = ModelIdentity(model_id="grok-4.5", effort="xhigh", fast=False)
    assert match_option(grok_4_5_at_xhigh, GROK_CATALOG.options) is None


def test_a_pick_may_leave_the_effort_unnamed_to_run_at_the_models_default() -> None:
    assert validate_model_pick(GROK_CATALOG.options, "grok-4.5", None, False).id == "grok-4.5"


def test_the_bar_reads_the_model_state_file_mngr_writes_for_grok(tmp_path: Path) -> None:
    model_state_path = get_model_state_path(HarnessType.GROK, tmp_path)
    assert model_state_path == tmp_path / MODEL_STATE_FILENAME
    assert read_model_identity(model_state_path) is None

    model_state_path.write_text(json.dumps({"model": "grok-4.6", "effort": "xhigh", "fast": False}))

    identity = read_model_identity(model_state_path)
    assert identity == ModelIdentity(model_id="grok-4.6", effort="xhigh", fast=False)
    assert resolve_model_choice(identity, GROK_CATALOG.options).matched == GROK_CATALOG.options[0]
