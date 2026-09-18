from typing import Final

from imbue.imbue_common.pure import pure

# OVH Object Storage endpoints the boxes transfer workspace stop/start artifacts
# to, pinned to their IPv4 addresses in /etc/hosts. The in-DC IPv6 path to these
# VIPs intermittently blackholes TCP flows for tens of seconds under the boxes'
# 1 Gbps QoS (observed in both the vin and hil datacenters), which starves
# uploads to ~2-30 MB/s and trips server-side RequestTimeout errors, while IPv4
# sustains the full provisioned 1 Gbps. Go S3 clients (s5cmd) prefer IPv6 but
# honor /etc/hosts, so the pin routes every transfer over IPv4. A 1-minute
# systemd timer re-resolves the A records so a changed VIP heals within a
# minute.
# CLEANUP: drop the pin machinery (this module, its two call sites in
# bare_metal_prep, bind9-dnsutils in both apt lists, its entries in the
# telemetry integrity manifest, and their tests) once OVH ticket #723301
# confirms the in-DC IPv6 path to Object Storage no longer blackholes flows.
_S3_IPV4_PIN_HOSTNAMES: Final[tuple[str, ...]] = (
    "s3.us-east-va.io.cloud.ovh.us",
    "s3.us-west-or.io.cloud.ovh.us",
)

# The root-owned pin artifacts prep installs on every box. Listed here (the
# renderer below is their single writer) so the gen-2 telemetry integrity
# manifest can hash them: the script decides the address every workspace
# stop/start artifact is uploaded to and restored from, and its units are a
# root-run-every-minute hook, so a change to any of them must signal.
S3_IPV4_PIN_SCRIPT_PATH: Final[str] = "/usr/local/sbin/mngr-s3-ipv4-pin.sh"
S3_IPV4_PIN_SERVICE_PATH: Final[str] = "/etc/systemd/system/mngr-s3-ipv4-pin.service"
S3_IPV4_PIN_TIMER_PATH: Final[str] = "/etc/systemd/system/mngr-s3-ipv4-pin.timer"
S3_IPV4_PIN_ARTIFACT_PATHS: Final[tuple[str, ...]] = (
    S3_IPV4_PIN_SCRIPT_PATH,
    S3_IPV4_PIN_SERVICE_PATH,
    S3_IPV4_PIN_TIMER_PATH,
)


@pure
def render_s3_ipv4_pin_section() -> str:
    """The OVH Object Storage IPv4 pin (script, oneshot unit, minutely timer) shared by both generations' preps."""
    s3_pin_hostnames = " ".join(_S3_IPV4_PIN_HOSTNAMES)
    return f"""\
# Pin the OVH Object Storage endpoints to their IPv4 addresses via a managed
# /etc/hosts block, re-resolved every minute by a systemd timer. The in-DC
# IPv6 path blackholes flows (OVH ticket #723301); full rationale on
# _S3_IPV4_PIN_HOSTNAMES in s3_ipv4_pin.py.
cat > {S3_IPV4_PIN_SCRIPT_PATH} <<'MNGR_S3_PIN'
#!/bin/bash
# Managed by mngr (bare_metal_prep): pin the OVH Object Storage endpoints to
# their IPv4 addresses so box->S3 transfers never ride the in-DC IPv6 path,
# which intermittently blackholes flows (OVH ticket #723301). Re-run every
# minute by mngr-s3-ipv4-pin.timer; a failed lookup keeps the current pin.
set -euo pipefail
hosts_file=/etc/hosts
begin_mark="# BEGIN mngr-s3-ipv4-pin (managed block, do not edit)"
end_mark="# END mngr-s3-ipv4-pin"
pin_lines=""
for endpoint_hostname in {s3_pin_hostnames}; do
    # dig queries DNS directly (it never reads the hosts file), so the
    # re-resolution is not poisoned by the existing pin.
    address=$(dig +short +time=3 +tries=2 A "$endpoint_hostname" 2>/dev/null | grep -Em1 '^[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+$' || true)
    if [ -z "$address" ]; then
        # Journal-visible so a persistent re-resolution failure (a stale pin
        # after a VIP change) does not go unnoticed.
        echo "mngr-s3-ipv4-pin: failed to resolve $endpoint_hostname; falling back to the existing pin" >&2
        address=$(sed -n "/^$begin_mark\\$/,/^$end_mark\\$/p" "$hosts_file" | awk -v h="$endpoint_hostname" '$2 == h {{print $1}}' | head -n1)
    fi
    if [ -n "$address" ]; then
        pin_lines="$pin_lines$address $endpoint_hostname"$'\\n'
    fi
done
without_block=$(awk -v b="$begin_mark" -v e="$end_mark" '$0 == b {{skip=1}} !skip {{print}} $0 == e {{skip=0}}' "$hosts_file")
# Per-process temp name: enabling the timer fires the service immediately (its
# OnBootSec point has already elapsed), so that run races prep's synchronous
# seed run; a shared temp path would make the losing run's mv fail.
tmp_file="$hosts_file.mngr-s3-pin-tmp.$$"
printf '%s\\n%s\\n%s%s\\n' "$without_block" "$begin_mark" "$pin_lines" "$end_mark" > "$tmp_file"
if cmp -s "$tmp_file" "$hosts_file"; then
    rm -f "$tmp_file"
else
    chmod 644 "$tmp_file"
    mv "$tmp_file" "$hosts_file"
fi
MNGR_S3_PIN
chmod +x {S3_IPV4_PIN_SCRIPT_PATH}
cat > {S3_IPV4_PIN_SERVICE_PATH} <<'MNGR_S3_PIN_UNIT'
[Unit]
Description=Refresh the IPv4 hosts-file pin for OVH Object Storage endpoints

[Service]
Type=oneshot
ExecStart={S3_IPV4_PIN_SCRIPT_PATH}
MNGR_S3_PIN_UNIT
cat > {S3_IPV4_PIN_TIMER_PATH} <<'MNGR_S3_PIN_TIMER'
[Unit]
Description=Re-resolve the OVH Object Storage IPv4 pin every minute

[Timer]
OnBootSec=30s
OnUnitActiveSec=1min
AccuracySec=15s

[Install]
WantedBy=timers.target
MNGR_S3_PIN_TIMER
systemctl daemon-reload
systemctl enable --now mngr-s3-ipv4-pin.timer
# Seed the pin synchronously so the first transfer after prep already rides IPv4.
{S3_IPV4_PIN_SCRIPT_PATH}
"""
