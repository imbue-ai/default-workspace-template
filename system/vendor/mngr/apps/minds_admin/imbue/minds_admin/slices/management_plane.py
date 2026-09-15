"""Gen-2 management-plane renderers: box WireGuard bring-up and the ``:22`` lockdown.

Phase 3 of specs/slice-fleet-gen2. Gen-2 boxes expose their management sshd
only to the tier's Modal Proxy static IPs (the connector's egress) and to the
tier's operator WireGuard overlay; everything here is a pure renderer consumed
by the gen-2 box prep (``minds-admin server prep`` / ``setup``) and the
``minds-admin wireguard`` commands.

The overlay addressing plan lives in the minds config
(``MANAGEMENT_OVERLAY_CIDR_BY_TIER``): every tier's overlay is a disjoint
carve of the reserved ``10.64.0.0/10`` supernet. Operators occupy the first
/24 of the tier's overlay and are assigned by hand in the ``[management_plane]``
table of the tier's committed ``deploy.toml``; boxes are assigned sequentially above it at prep
and stamped on their ``bare_metal_servers`` row.
"""

import ipaddress
from collections.abc import Sequence
from typing import AbstractSet
from typing import Final

from imbue.imbue_common.pure import pure
from imbue.minds.config.data_types import ManagementOverlayAllocation
from imbue.minds.config.data_types import WireguardOperatorConfig
from imbue.mngr_imbue_cloud.data_types import BareMetalServer
from imbue.mngr_imbue_cloud.errors import BareMetalConfigError

# Box-side WireGuard material paths. The private key is generated on the box
# at prep and never leaves it; prep echoes the PUBLIC key back (see
# ``WIREGUARD_PUBLIC_KEY_MARKER``) so the CLI can stamp it on the box's row.
_WIREGUARD_PRIVATE_KEY_PATH: Final[str] = "/etc/wireguard/wg0.key"
_WIREGUARD_PUBLIC_KEY_PATH: Final[str] = "/etc/wireguard/wg0.pub"
WIREGUARD_CONFIG_PATH: Final[str] = "/etc/wireguard/wg0.conf"
WIREGUARD_PUBLIC_KEY_MARKER: Final[str] = "MNGR_WIREGUARD_PUBLIC_KEY"

# The management lockdown's nftables policy: its own table (never the
# per-VM ``mngr_slices`` table, which the slice helper owns), loaded from a
# prep-installed file so it survives reboots via nftables.service.
MANAGEMENT_NFT_TABLE: Final[str] = "mngr_mgmt"
_MANAGEMENT_NFT_POLICY_PATH: Final[str] = "/etc/nftables.d/mngr-management.nft"


@pure
def next_free_box_wireguard_address(
    assigned_addresses: AbstractSet[str], allocation: ManagementOverlayAllocation
) -> str:
    """The lowest free box overlay address: sequential from the first host after the operator block.

    ``assigned_addresses`` is every ``wireguard_address`` already stamped on a
    ``bare_metal_servers`` row (any tier state); operator addresses live in
    their own reserved block and are skipped entirely.
    """
    for candidate in allocation.overlay.hosts():
        if candidate in allocation.operator_block:
            continue
        # .0 / .255 are valid /32 peers inside the overlay, but skipping them
        # keeps every assigned address unambiguous to humans and tooling.
        if int(candidate) % 256 in (0, 255):
            continue
        if str(candidate) not in assigned_addresses:
            return str(candidate)
    raise BareMetalConfigError(f"management overlay {allocation.overlay} has no free box address left")


@pure
def resolve_box_overlay_address(
    current_address: str | None,
    assigned_addresses: AbstractSet[str],
    allocation: ManagementOverlayAllocation,
) -> str:
    """The box's overlay address: its stamped one when it fits the tier's allocation, else the next free.

    A stamped address outside the tier's overlay (or inside its operator
    block) means the fleet's addressing plan changed since the box was
    prepped; the box is renumbered so a re-prep converges the fleet onto the
    current plan.
    """
    if current_address is not None:
        candidate = ipaddress.IPv4Address(current_address)
        if candidate in allocation.overlay and candidate not in allocation.operator_block:
            return current_address
    return next_free_box_wireguard_address(assigned_addresses, allocation)


@pure
def _render_wireguard_peer_blocks(operators: Sequence[WireguardOperatorConfig]) -> str:
    blocks = []
    for operator in operators:
        blocks.append(
            f"# operator {operator.name}\n"
            f"[Peer]\n"
            f"PublicKey = {operator.public_key}\n"
            f"AllowedIPs = {operator.address}/32\n"
        )
    return "\n".join(blocks)


@pure
def render_wireguard_prep_section(
    *,
    wireguard_address: str,
    listen_port: int,
    operators: Sequence[WireguardOperatorConfig],
    overlay_prefix_length: int,
) -> str:
    """The idempotent root bash section that brings up the box's management WireGuard.

    Generates the box's keypair once (the private key never leaves the box),
    renders ``wg0.conf`` with the tier's operator peers, and restarts the
    interface only when the rendered config actually changed (so a re-prep
    with an unchanged peer list never bounces live operator sessions). Always
    echoes the box's public key as ``MNGR_WIREGUARD_PUBLIC_KEY <key>`` so the caller
    can stamp it on the box's row. Also the whole body of ``minds-admin
    wireguard sync-peers`` (which re-runs it over management SSH).
    """
    peer_blocks = _render_wireguard_peer_blocks(operators)
    return f"""\
# Management-plane WireGuard: the operator path to this box once :22 locks
# down. The keypair is generated here on first prep and never rotated by
# re-runs; peers come from the tier's committed deploy.toml [management_plane] table.
# The tight umask is scoped to the key-material writes (restored below) so
# later prep sections create files with their normal modes.
previous_umask=$(umask)
umask 077
mkdir -p /etc/wireguard
if [ ! -f {_WIREGUARD_PRIVATE_KEY_PATH} ]; then
    wg genkey > {_WIREGUARD_PRIVATE_KEY_PATH}
fi
wg pubkey < {_WIREGUARD_PRIVATE_KEY_PATH} > {_WIREGUARD_PUBLIC_KEY_PATH}
cat > {WIREGUARD_CONFIG_PATH}.mngr-tmp <<'MNGR_WG_CONF'
[Interface]
Address = {wireguard_address}/{overlay_prefix_length}
ListenPort = {listen_port}
PrivateKey = __MNGR_WG_PRIVATE_KEY__

{peer_blocks}
MNGR_WG_CONF
# The private key is spliced in post-heredoc so the rendered laptop-side
# script never contains it (WireGuard keys are base64: no sed metacharacters).
sed -i "s|__MNGR_WG_PRIVATE_KEY__|$(cat {_WIREGUARD_PRIVATE_KEY_PATH})|" {WIREGUARD_CONFIG_PATH}.mngr-tmp
umask "$previous_umask"
systemctl enable wg-quick@wg0
if ! cmp -s {WIREGUARD_CONFIG_PATH}.mngr-tmp {WIREGUARD_CONFIG_PATH} 2>/dev/null; then
    mv {WIREGUARD_CONFIG_PATH}.mngr-tmp {WIREGUARD_CONFIG_PATH}
    systemctl restart wg-quick@wg0
else
    rm -f {WIREGUARD_CONFIG_PATH}.mngr-tmp
    systemctl start wg-quick@wg0
fi
echo "{WIREGUARD_PUBLIC_KEY_MARKER} $(cat {_WIREGUARD_PUBLIC_KEY_PATH})"
"""


@pure
def render_management_nftables_policy(proxy_static_ips: Sequence[str]) -> str:
    """The ``mngr_mgmt`` nftables policy file: box ``:22`` default-drop with two allowed paths.

    Scoped entirely to the management sshd port -- the WireGuard port and the public
    per-slice port range ride the chain's accept policy untouched, and DNAT'd
    slice traffic traverses the forward hook, never input. The
    established/related exemption comes first so applying the policy never
    severs the SSH session applying it. Uses the add-then-delete-then-add
    idempotent replace pattern so re-loading the file converges instead of
    erroring or duplicating rules.
    """
    if not proxy_static_ips:
        raise BareMetalConfigError("the management lockdown policy needs at least one proxy static IP")
    for raw_ip in proxy_static_ips:
        ipaddress.IPv4Address(raw_ip)
    allowed_ips = ", ".join(str(ip) for ip in proxy_static_ips)
    return f"""\
#!/usr/sbin/nft -f
# Managed by mngr (gen-2 management-plane lockdown; specs/slice-fleet-gen2).
# Box :22 answers only the tier's Modal Proxy static IPs (the connector's
# egress) and the wg0 management overlay. Separate from the per-VM
# mngr_slices table, which the slice helper owns.
add table inet {MANAGEMENT_NFT_TABLE}
delete table inet {MANAGEMENT_NFT_TABLE}
add table inet {MANAGEMENT_NFT_TABLE} {{
    chain input {{
        type filter hook input priority -10; policy accept;
        tcp dport 22 ct state established,related accept
        iifname "lo" tcp dport 22 accept
        iifname "wg0" tcp dport 22 accept
        ip saddr {{ {allowed_ips} }} tcp dport 22 accept
        tcp dport 22 counter drop
    }}
}}
"""


@pure
def render_nftables_persistence_prep_section() -> str:
    """The idempotent root bash section making every policy file under ``/etc/nftables.d`` boot-persistent.

    Shared by the box-level policies prep installs (the ``:22`` lockdown and
    the slice DHCP server's udp/67 policy): ``nftables.service`` loads
    ``/etc/nftables.conf`` at boot, which is made to include the directory.
    The distro's ``/etc/nftables.conf`` ships a ``flush ruleset`` line; a
    service restart running it would wipe the live per-VM ``mngr_slices``
    rules out from under running slices, so it is neutralized here.
    """
    return """\
# Boot persistence for the box-level nftables policies: nftables.service
# loads /etc/nftables.conf, which includes every policy file under
# /etc/nftables.d. The distro conf's `flush ruleset` would wipe the live
# per-VM slice rules on a service restart, so it is neutralized.
mkdir -p /etc/nftables.d
touch /etc/nftables.conf
sed -i 's/^flush ruleset$//' /etc/nftables.conf
grep -qxF 'include "/etc/nftables.d/*.nft"' /etc/nftables.conf \\
    || echo 'include "/etc/nftables.d/*.nft"' >> /etc/nftables.conf
systemctl enable nftables
"""


@pure
def render_management_lockdown_prep_section(proxy_static_ips: Sequence[str]) -> str:
    """The idempotent root bash section installing (or removing) the ``:22`` lockdown.

    With proxy IPs configured: install the policy file, make it boot-persistent
    through nftables.service (:func:`render_nftables_persistence_prep_section`),
    and apply it now. With none configured: converge the box back to open
    (remove the file and the live table) -- so clearing the tier's
    ``[management_plane.modal_proxy]`` block is a real rollback path.
    """
    if not proxy_static_ips:
        return f"""\
# Management-plane :22 lockdown: not configured for this tier (no Modal Proxy
# static IPs in the tier's deploy.toml [management_plane] table); converge the box to open.
rm -f {_MANAGEMENT_NFT_POLICY_PATH}
if nft list table inet {MANAGEMENT_NFT_TABLE} >/dev/null 2>&1; then
    nft delete table inet {MANAGEMENT_NFT_TABLE}
fi
"""
    policy_text = render_management_nftables_policy(proxy_static_ips)
    return f"""\
# Management-plane :22 lockdown: default-drop except the tier's Modal Proxy
# static IPs and the wg0 overlay. The live prep SSH session survives via the
# established/related exemption; NEW laptop connections need WireGuard from here on.
{render_nftables_persistence_prep_section()}\
cat > {_MANAGEMENT_NFT_POLICY_PATH} <<'MNGR_MGMT_NFT'
{policy_text}\
MNGR_MGMT_NFT
nft -f {_MANAGEMENT_NFT_POLICY_PATH}
"""


@pure
def parse_wireguard_public_key_from_prep_output(stdout: str) -> str | None:
    """Extract the box's WireGuard public key from a prep run's ``MNGR_WIREGUARD_PUBLIC_KEY`` marker line."""
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(WIREGUARD_PUBLIC_KEY_MARKER):
            parts = stripped.split()
            if len(parts) == 2:
                return parts[1]
    return None


@pure
def build_operator_wireguard_client_config(
    *,
    operator: WireguardOperatorConfig,
    tier: str,
    boxes: Sequence[BareMetalServer],
    listen_port: int,
) -> str:
    """Render the operator's wg-quick client config for a tier's gen-2 fleet.

    One ``[Peer]`` per box that has completed prep (address + public key
    stamped); the operator splices their own private key over the placeholder
    (it exists only on their machine). ``AllowedIPs`` is each box's overlay
    /32, so only management traffic to the boxes rides the tunnel.
    """
    peer_blocks = []
    for box in boxes:
        if not box.wireguard_address or not box.wireguard_public_key or not box.public_address:
            raise BareMetalConfigError(
                f"box {box.id} is missing wireguard_address / wireguard_public_key / public_address; "
                "filter to prepped gen-2 boxes before rendering the client config"
            )
        peer_blocks.append(
            f"# box {box.id} ({box.public_address})\n"
            f"[Peer]\n"
            f"PublicKey = {box.wireguard_public_key}\n"
            f"Endpoint = {box.public_address}:{listen_port}\n"
            f"AllowedIPs = {box.wireguard_address}/32\n"
            f"PersistentKeepalive = 25\n"
        )
    peers_text = "\n".join(peer_blocks) if peer_blocks else "# (no prepped gen-2 boxes yet)\n"
    # /32 on purpose: the client then installs only the per-box /32 routes
    # from AllowedIPs, so nothing else on the operator's machine (VPNs,
    # Docker, another tier's tunnel) can collide with a broad connected route.
    return f"""\
# minds '{tier}' management overlay -- operator {operator.name}
# Save as e.g. /etc/wireguard/mngr-{tier}.conf and run: wg-quick up mngr-{tier}
[Interface]
Address = {operator.address}/32
PrivateKey = <REPLACE_WITH_YOUR_PRIVATE_KEY>

{peers_text}"""
