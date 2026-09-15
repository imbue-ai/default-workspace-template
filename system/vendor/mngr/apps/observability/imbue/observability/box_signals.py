"""The gen-2 box telemetry signal vocabulary shared by the emitter and the alert rules.

The gen-2 box telemetry collector (rendered by ``minds_admin``'s
``slices/box_telemetry.py`` and installed by the gen-2 box prep) evaluates
every tier-1 condition ON the box -- thresholds, allowlists, and deltas all
live in PR-reviewed rendered code -- and emits one journald line per firing
condition, prefixed with :data:`BOX_SIGNAL_MARKER` and the signal name. The
OpenObserve alert rules (``alert_provisioning.py``) then only have to match
those marker lines in the ``box_logs`` stream, which keeps the server-side SQL
trivial and the alert payloads free of log content.

Both sides read the signal names from this enum so they can never drift.
"""

from enum import auto
from typing import Final

from imbue.imbue_common.enums import UpperCaseStrEnum

# The exact token the collector prefixes every signal line with. Alert rules
# match on "<marker> <signal name>", so the marker must never appear in any
# other log line the boxes emit.
BOX_SIGNAL_MARKER: Final[str] = "MNGR_BOX_SIGNAL"

# The journald identifier the collector's service unit stamps on every line.
# The alert rules filter on it (it lands in box_logs as the
# ``body_syslog_identifier`` column).
BOX_TELEMETRY_SYSLOG_IDENTIFIER: Final[str] = "mngr-box-telemetry"


class BoxTelemetrySignal(UpperCaseStrEnum):
    """One tier-1 condition the gen-2 box telemetry collector can raise.

    Each member is both the token in the emitted journald line and the basis
    of the corresponding OpenObserve alert rule's name.
    """

    # A slice VM attempted direct-to-MX SMTP (the per-VM nftables block's
    # named counter moved). Blocked attempts are themselves an abuse signal:
    # legitimate mail uses authenticated submission on 587/465.
    SMTP_BLOCKED = auto()
    # A slice VM's new-connection rate stayed above the alert threshold (half
    # the enforced 300/s ceiling) across a whole collection interval.
    NEW_CONNECTION_RATE = auto()
    # A slice VM's egress rate exceeded the configured share of the box's
    # declared uplink across a whole collection interval.
    EGRESS_RATE = auto()
    # The box's conntrack table is close to its ceiling (all tenants share it;
    # exhaustion silently breaks every VM's connectivity).
    CONNTRACK_PRESSURE = auto()
    # The uplink's negotiated speed disagrees with the declared uplink_mbps
    # the fair-share HTB classes are sized from.
    LINK_SPEED_MISMATCH = auto()
    # A root-owned prep artifact (template unit, slice helper, sudoers, wg
    # config) no longer matches the bytes prep installed.
    PREP_ARTIFACT_DRIFT = auto()
    # A slice qemu process has child processes. A healthy qemu spawns nothing
    # after boot (its seccomp sandbox denies spawn), so any child is a
    # compromise indicator.
    QEMU_CHILD_PROCESSES = auto()
    # The management sshd saw something a locked-down gen-2 box never
    # legitimately produces: a login from outside the proxy/wg allowlist, any
    # password authentication, or a direct root login.
    MANAGEMENT_SSH_ANOMALY = auto()
    # A sudo invocation outside the scoped grants (the slice service user may only drive the
    # per-ordinal slice units; slice users may not sudo at all).
    SUDO_ANOMALY = auto()
    # systemd OOM-killed a slice VM's unit: the hardened unit's MemoryMax
    # backstop fired. It must never fire in practice (memory pressure resolves
    # inside the workspace container, via earlyoom and the container's own
    # cgroup limit), so a firing means a VM's real footprint outgrew its
    # budget -- the per-VM overhead constant, or the guest, is wrong.
    SLICE_UNIT_OOM_KILLED = auto()
    # The gen-2 storage root is not mounted from its opened LUKS mapper: the
    # box's TPM unlock failed at boot (every slice on it is down until
    # `minds-admin server unlock` opens the volume by its recovery
    # passphrase), or the box was never encrypted and its slices sit in
    # plaintext.
    STORAGE_VOLUME_LOCKED = auto()


def box_signal_line_prefix(signal: BoxTelemetrySignal) -> str:
    """The exact prefix of an emitted signal line (what the alert SQL matches)."""
    return f"{BOX_SIGNAL_MARKER} {signal}"
