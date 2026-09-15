import subprocess
from datetime import datetime
from datetime import timezone

import pytest

from imbue.minds.config.data_types import WireguardOperatorConfig
from imbue.minds.config.data_types import management_overlay_for_tier
from imbue.minds_admin.slices.management_plane import build_operator_wireguard_client_config
from imbue.minds_admin.slices.management_plane import next_free_box_wireguard_address
from imbue.minds_admin.slices.management_plane import parse_wireguard_public_key_from_prep_output
from imbue.minds_admin.slices.management_plane import render_management_lockdown_prep_section
from imbue.minds_admin.slices.management_plane import render_management_nftables_policy
from imbue.minds_admin.slices.management_plane import render_wireguard_prep_section
from imbue.minds_admin.slices.management_plane import resolve_box_overlay_address
from imbue.mngr_imbue_cloud.data_types import BareMetalServer
from imbue.mngr_imbue_cloud.errors import BareMetalConfigError
from imbue.mngr_imbue_cloud.primitives import BareMetalServerDbId
from imbue.mngr_imbue_cloud.primitives import BareMetalServerStatus
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_READY


def _assert_bash_syntax_ok(script: str) -> None:
    result = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True)
    assert result.returncode == 0, f"bash -n failed: {result.stderr}"


def _operators() -> tuple[WireguardOperatorConfig, ...]:
    return (
        WireguardOperatorConfig.model_validate({"name": "josh", "public_key": "opkeyjosh=", "address": "10.112.0.2"}),
        WireguardOperatorConfig.model_validate({"name": "alex", "public_key": "opkeyalex=", "address": "10.112.0.3"}),
    )


_DEV_ALLOCATION = management_overlay_for_tier("dev")


def test_next_free_box_wireguard_address_skips_the_operator_block() -> None:
    # The first box address comes after the whole 10.112.0.0/24 operator block
    # (and skips the ambiguous-looking .0 address).
    assert next_free_box_wireguard_address(set(), _DEV_ALLOCATION) == "10.112.1.1"


def test_next_free_box_wireguard_address_is_sequential_over_assigned_addresses() -> None:
    assigned = {"10.112.1.1", "10.112.1.2", "10.112.1.4"}
    assert next_free_box_wireguard_address(assigned, _DEV_ALLOCATION) == "10.112.1.3"


def test_resolve_box_overlay_address_keeps_an_in_plan_address() -> None:
    assert resolve_box_overlay_address("10.112.1.7", {"10.112.1.7"}, _DEV_ALLOCATION) == "10.112.1.7"


def test_resolve_box_overlay_address_renumbers_an_out_of_plan_address() -> None:
    # The dev canary was originally numbered from the pre-supernet 10.202.0.0/16
    # plan; a re-prep must converge it onto the tier's current allocation.
    assert resolve_box_overlay_address("10.202.1.1", {"10.202.1.1"}, _DEV_ALLOCATION) == "10.112.1.1"


def test_resolve_box_overlay_address_renumbers_an_address_inside_the_operator_block() -> None:
    assert resolve_box_overlay_address("10.112.0.9", {"10.112.0.9"}, _DEV_ALLOCATION) == "10.112.1.1"


def test_wireguard_prep_section_generates_once_and_echoes_the_public_key() -> None:
    section = render_wireguard_prep_section(
        wireguard_address="10.112.1.5", listen_port=51820, operators=_operators(), overlay_prefix_length=16
    )

    _assert_bash_syntax_ok(section)
    # Key material is generated only when absent (never rotated by a re-prep)
    # and the public half is echoed for the caller to stamp on the row.
    assert "if [ ! -f /etc/wireguard/wg0.key ]" in section
    assert "wg genkey" in section
    assert 'echo "MNGR_WIREGUARD_PUBLIC_KEY $(cat /etc/wireguard/wg0.pub)"' in section
    # The rendered laptop-side script must never embed a private key: the conf
    # carries a placeholder spliced on-box.
    assert "__MNGR_WG_PRIVATE_KEY__" in section
    # One peer block per operator, pinned to their /32.
    assert "PublicKey = opkeyjosh=" in section
    assert "AllowedIPs = 10.112.0.2/32" in section
    assert "PublicKey = opkeyalex=" in section
    assert "AllowedIPs = 10.112.0.3/32" in section
    assert "Address = 10.112.1.5/16" in section
    assert "ListenPort = 51820" in section
    # The key-material umask is scoped: tightened for the writes, restored so
    # later prep sections (extra prep scripts included) keep their normal modes.
    assert "previous_umask=$(umask)" in section
    assert "umask 077" in section
    assert 'umask "$previous_umask"' in section


def test_wireguard_prep_section_restarts_only_on_config_change() -> None:
    section = render_wireguard_prep_section(
        wireguard_address="10.112.1.5", listen_port=51820, operators=(), overlay_prefix_length=16
    )

    assert "cmp -s" in section
    assert "systemctl restart wg-quick@wg0" in section
    # The unchanged branch must not bounce a live interface (operators may be
    # connected over it while sync-peers runs).
    assert "systemctl start wg-quick@wg0" in section


def test_management_nftables_policy_scopes_the_drop_to_port_22() -> None:
    policy = render_management_nftables_policy(("203.0.113.10", "203.0.113.11"))

    # Idempotent replace pattern, own table (never the slice helper's).
    assert "add table inet mngr_mgmt" in policy
    assert "delete table inet mngr_mgmt" in policy
    # Established sessions survive the policy landing; WireGuard + proxy IPs are the
    # only new-connection paths; everything else on :22 drops.
    assert "tcp dport 22 ct state established,related accept" in policy
    assert 'iifname "wg0" tcp dport 22 accept' in policy
    assert "ip saddr { 203.0.113.10, 203.0.113.11 } tcp dport 22 accept" in policy
    assert "tcp dport 22 counter drop" in policy
    # Nothing but :22 is filtered: the WireGuard port and the slice port range ride
    # the accept policy.
    assert policy.count("dport") == policy.count("dport 22")


def test_management_nftables_policy_rejects_bad_inputs() -> None:
    with pytest.raises(BareMetalConfigError):
        render_management_nftables_policy(())
    with pytest.raises(ValueError):
        render_management_nftables_policy(("not-an-ip",))


def test_lockdown_section_installs_persistently_and_neutralizes_flush_ruleset() -> None:
    section = render_management_lockdown_prep_section(("203.0.113.10",))

    _assert_bash_syntax_ok(section)
    assert "cat > /etc/nftables.d/mngr-management.nft" in section
    # Boot persistence rides nftables.service; the distro conf's `flush
    # ruleset` would wipe the live per-VM slice tables on a service restart.
    assert "sed -i 's/^flush ruleset$//' /etc/nftables.conf" in section
    assert 'include "/etc/nftables.d/*.nft"' in section
    assert "systemctl enable nftables" in section
    assert "nft -f /etc/nftables.d/mngr-management.nft" in section


def test_lockdown_section_without_proxy_ips_converges_the_box_to_open() -> None:
    section = render_management_lockdown_prep_section(())

    _assert_bash_syntax_ok(section)
    assert "rm -f /etc/nftables.d/mngr-management.nft" in section
    assert "nft delete table inet mngr_mgmt" in section


def test_parse_wireguard_public_key_from_prep_output_finds_the_marker_line() -> None:
    stdout = (
        "installed /etc/systemd/system/mngr-slice@.service\nMNGR_WIREGUARD_PUBLIC_KEY boxpub123=\nMNGR_BOX_PREP_DONE\n"
    )
    assert parse_wireguard_public_key_from_prep_output(stdout) == "boxpub123="


def test_parse_wireguard_public_key_from_prep_output_returns_none_when_absent_or_malformed() -> None:
    assert parse_wireguard_public_key_from_prep_output("MNGR_BOX_PREP_DONE\n") is None
    assert parse_wireguard_public_key_from_prep_output("MNGR_WIREGUARD_PUBLIC_KEY\n") is None
    assert parse_wireguard_public_key_from_prep_output("MNGR_WIREGUARD_PUBLIC_KEY a b\n") is None


def _prepped_box(
    server_id: str, public_address: str, wireguard_address: str, wireguard_public_key: str
) -> BareMetalServer:
    return BareMetalServer(
        id=BareMetalServerDbId(server_id),
        plan_code="24rise02-v1-us",
        region="vin",
        public_address=public_address,
        slot_count=8,
        status=BareMetalServerStatus(SERVER_STATUS_READY),
        created_at=datetime(2026, 8, 23, tzinfo=timezone.utc),
        updated_at=datetime(2026, 8, 23, tzinfo=timezone.utc),
        box_generation=2,
        uplink_mbps=1000,
        wireguard_address=wireguard_address,
        wireguard_public_key=wireguard_public_key,
    )


def test_operator_client_config_has_one_pinned_peer_per_box() -> None:
    boxes = [
        _prepped_box("srv-1", "198.51.100.1", "10.112.1.10", "boxkey1="),
        _prepped_box("srv-2", "198.51.100.2", "10.112.1.1", "boxkey2="),
    ]

    config_text = build_operator_wireguard_client_config(
        operator=_operators()[0], tier="dev", boxes=boxes, listen_port=51820
    )

    # /32 on purpose: only the per-box AllowedIPs /32 routes land on the
    # operator machine, never a broad connected route.
    assert "Address = 10.112.0.2/32" in config_text
    assert "PrivateKey = <REPLACE_WITH_YOUR_PRIVATE_KEY>" in config_text
    assert "PublicKey = boxkey1=" in config_text
    assert "Endpoint = 198.51.100.1:51820" in config_text
    assert "AllowedIPs = 10.112.1.10/32" in config_text
    assert "PublicKey = boxkey2=" in config_text
    assert "Endpoint = 198.51.100.2:51820" in config_text
    assert "AllowedIPs = 10.112.1.1/32" in config_text


def test_operator_client_config_refuses_an_unprepped_box() -> None:
    unprepped = _prepped_box("srv-3", "198.51.100.3", "", "")

    with pytest.raises(BareMetalConfigError, match="missing wireguard_address"):
        build_operator_wireguard_client_config(
            operator=_operators()[0], tier="dev", boxes=[unprepped], listen_port=51820
        )
