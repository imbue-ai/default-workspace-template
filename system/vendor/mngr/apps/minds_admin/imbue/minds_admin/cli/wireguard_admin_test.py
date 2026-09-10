import click
import pytest

from imbue.minds.config.data_types import ManagementPlaneConfig
from imbue.minds.config.data_types import WireguardOperatorConfig
from imbue.minds_admin.cli.wireguard_admin import _select_operator


def _config(*operators: WireguardOperatorConfig) -> ManagementPlaneConfig:
    return ManagementPlaneConfig.model_validate({"wireguard": {"operators": [op.model_dump() for op in operators]}})


_JOSH = WireguardOperatorConfig.model_validate({"name": "josh", "public_key": "opkeyjosh=", "address": "10.202.0.2"})
_ALEX = WireguardOperatorConfig.model_validate({"name": "alex", "public_key": "opkeyalex=", "address": "10.202.0.3"})


def test_select_operator_defaults_to_the_only_configured_one() -> None:
    assert _select_operator(_config(_JOSH), None) == _JOSH


def test_select_operator_requires_a_name_when_several_are_configured() -> None:
    with pytest.raises(click.UsageError, match="--operator is required"):
        _select_operator(_config(_JOSH, _ALEX), None)


def test_select_operator_picks_by_name_and_rejects_unknown_names() -> None:
    assert _select_operator(_config(_JOSH, _ALEX), "alex") == _ALEX
    with pytest.raises(click.UsageError, match="no operator 'sam'"):
        _select_operator(_config(_JOSH, _ALEX), "sam")


def test_select_operator_refuses_an_empty_operator_list() -> None:
    with pytest.raises(click.ClickException, match="lists no"):
        _select_operator(_config(), None)
