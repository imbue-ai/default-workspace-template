import click
import pytest
from inline_snapshot import snapshot

from imbue.minds.config.data_types import ManagementPlaneConfig
from imbue.minds.config.data_types import WireguardOperatorConfig
from imbue.minds.config.loader import committed_deploy_config_tiers
from imbue.minds.config.loader import load_deploy_config
from imbue.minds_admin.cli.wireguard_admin import MANAGEMENT_TIERS
from imbue.minds_admin.cli.wireguard_admin import PeerSyncOutcome
from imbue.minds_admin.cli.wireguard_admin import _select_operator
from imbue.minds_admin.cli.wireguard_admin import build_peer_sync_report
from imbue.minds_admin.cli.wireguard_admin import live_peer_mismatch_or_none
from imbue.minds_admin.cli.wireguard_admin import resolve_management_tier
from imbue.minds_admin.cli.wireguard_admin import select_canary_box
from imbue.minds_admin.slices.testing import make_ready_gen1_server


def _config(*operators: WireguardOperatorConfig) -> ManagementPlaneConfig:
    return ManagementPlaneConfig.model_validate({"wireguard": {"operators": [op.model_dump() for op in operators]}})


_JOSH = WireguardOperatorConfig.model_validate(
    {"name": "josh", "public_key": "wee0+EFoclrCL2Pdf3oT3dKtL3Z2W2Tr9JsbvLzqwLc=", "address": "10.202.0.2"}
)
_ALEX = WireguardOperatorConfig.model_validate(
    {"name": "alex", "public_key": "aAWhPfhifGs/d9CO0mkiyJc96qHKK8mmeiM7UXSAi3g=", "address": "10.202.0.3"}
)


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


@pytest.mark.parametrize(
    ("explicit_tier", "activated_tier", "expected"),
    [("dev", "staging", "dev"), ("dev", None, "dev"), (None, "staging", "staging")],
)
def test_resolve_management_tier_prefers_the_explicit_flag_then_the_activated_env(
    explicit_tier: str | None, activated_tier: str | None, expected: str
) -> None:
    assert resolve_management_tier(explicit_tier, activated_tier) == expected


def test_resolve_management_tier_refuses_without_a_flag_or_an_activated_env() -> None:
    with pytest.raises(click.UsageError, match="pass --tier"):
        resolve_management_tier(None, None)


def test_management_tiers_cover_every_tier_with_a_committed_deploy_config() -> None:
    # Every tier the flag offers must load, or `--tier <x>` would fail after
    # click accepted it; every committed tier must be offered, or an operator
    # could not address that tier's fleet without activating one of its envs.
    assert sorted(MANAGEMENT_TIERS) == committed_deploy_config_tiers()
    for tier in MANAGEMENT_TIERS:
        load_deploy_config(tier)


def test_build_peer_sync_report_keys_boxes_by_row_id_and_carries_errors_only_for_failures() -> None:
    outcomes = [
        PeerSyncOutcome(server_id="box-a", address="127.0.0.1", is_synced=True),
        PeerSyncOutcome(server_id="box-b", address="203.0.113.7", is_synced=False, error="ssh exited 255"),
    ]

    report = build_peer_sync_report("dev", (_JOSH, _ALEX), outcomes)

    assert report == snapshot(
        {
            "tier": "dev",
            "operators": ["josh", "alex"],
            "boxes": {
                "box-a": {"synced": True, "address": "127.0.0.1"},
                "box-b": {"synced": False, "address": "203.0.113.7", "error": "ssh exited 255"},
            },
        }
    )


def test_live_peer_mismatch_or_none_accepts_exactly_the_committed_keys_in_any_order() -> None:
    live_output = f"{_ALEX.public_key}\n{_JOSH.public_key}\n"

    assert live_peer_mismatch_or_none(live_output, (_JOSH, _ALEX)) is None


def test_live_peer_mismatch_or_none_names_missing_and_unexpected_keys() -> None:
    # A box whose wg0 came back without a committed peer (or with one that was
    # removed) has not converged, whatever the sync session's exit said.
    live_output = f"{_JOSH.public_key}\nstale-key=\n"

    mismatch = live_peer_mismatch_or_none(live_output, (_JOSH, _ALEX))

    assert mismatch is not None
    assert str(_ALEX.public_key) in mismatch
    assert "stale-key=" in mismatch


def test_live_peer_mismatch_or_none_treats_an_empty_peer_list_as_a_mismatch() -> None:
    assert live_peer_mismatch_or_none("", (_JOSH,)) is not None


def test_select_canary_box_defaults_to_the_first_box_and_honors_an_explicit_id() -> None:
    first = make_ready_gen1_server()
    second = make_ready_gen1_server()

    assert select_canary_box([first, second], None) == first
    assert select_canary_box([first, second], str(second.id)) == second


def test_select_canary_box_refuses_an_id_outside_the_fleet() -> None:
    with pytest.raises(click.UsageError, match="not a prepped gen-2 box"):
        select_canary_box([make_ready_gen1_server()], "not-a-box")
