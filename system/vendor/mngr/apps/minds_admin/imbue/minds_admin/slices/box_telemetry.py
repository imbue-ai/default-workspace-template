"""Gen-2 box telemetry renderers: the on-box tier-1 collector and its prep section.

Phase 4 of specs/slice-fleet-gen2. The collector is a prep-installed python3
script on a systemd timer that evaluates every tier-1 condition ON the box --
per-VM nftables counters (settled decision: no third-party nftables exporter),
the declared-vs-negotiated link-speed audit, prep-artifact integrity, the
qemu-children tripwire, and the management-plane auth-log checks -- and emits
JSON lines to journald, which the existing otelcol pipeline already ships into
the tier's ``box_logs`` stream.

Conditions that warrant an alert are additionally emitted as
``MNGR_BOX_SIGNAL <name>`` marker lines (the vocabulary lives in
``imbue.observability.box_signals``), so the server-side OpenObserve rules
(``imbue.observability.alert_provisioning``) are trivial substring matches and
all thresholds/allowlists stay in this PR-reviewed rendered code.
"""

import json
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Final

from imbue.imbue_common.pure import pure
from imbue.minds_admin.slices.management_plane import WIREGUARD_CONFIG_PATH
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_BY_ORDINAL_DIR
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_DHCP_CONFIG_PATH
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_DHCP_NFT_POLICY_PATH
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_DHCP_UNIT_PATH
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_HELPER_PATH
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_MAX_NEW_CONNECTIONS_PER_SECOND
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_MAX_SLICE_COUNT
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_NFT_TABLE
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_SLICE_SERVICE_USER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_SUDOERS_PATH
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_UNIT_PATH
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import slice_unit_name
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import SSH_CA_BOX_BOOTSTRAP_USER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import SSH_CA_PUBLIC_KEY_PATH
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import SSH_CA_SSHD_DROP_IN_PATH
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import ssh_ca_principals_file_path
from imbue.mngr_imbue_cloud.slices.qemu_slice import GEN2_SLICE_SUDO_VERBS
from imbue.observability.box_signals import BOX_SIGNAL_MARKER
from imbue.observability.box_signals import BOX_TELEMETRY_SYSLOG_IDENTIFIER
from imbue.observability.box_signals import BoxTelemetrySignal

# Where the collector's pieces live on the box (all prep-installed,
# content-converged like the other gen-2 prep artifacts).
BOX_TELEMETRY_SCRIPT_PATH: Final[str] = "/usr/local/sbin/mngr-box-telemetry"
BOX_TELEMETRY_SERVICE_PATH: Final[str] = "/etc/systemd/system/mngr-box-telemetry.service"
BOX_TELEMETRY_TIMER_PATH: Final[str] = "/etc/systemd/system/mngr-box-telemetry.timer"
# Root-owned state: counter snapshots for deltas, the journal cursor, and the
# prep-artifact hash manifest prep records after installing the artifacts.
BOX_TELEMETRY_STATE_DIR: Final[str] = "/var/lib/mngr-box-telemetry"
PREP_ARTIFACT_MANIFEST_PATH: Final[str] = f"{BOX_TELEMETRY_STATE_DIR}/prep-artifacts.sha256"

# The root-owned artifacts whose installed bytes the integrity check compares
# against prep's manifest. wg0.conf is included: prep installs it too (with
# the on-box private key spliced in), so its manifest hash is taken from the
# installed file rather than a pure renderer. The slice DHCP server's config,
# unit and udp/67 policy are covered because they decide which address every
# guest gets and who may talk to the server.
# The collector's own script and units are covered too (tampering with the
# watcher must itself signal).
PREP_ARTIFACT_MANIFEST_TARGETS: Final[tuple[str, ...]] = (
    GEN2_UNIT_PATH,
    GEN2_HELPER_PATH,
    GEN2_SUDOERS_PATH,
    GEN2_DHCP_CONFIG_PATH,
    GEN2_DHCP_UNIT_PATH,
    GEN2_DHCP_NFT_POLICY_PATH,
    WIREGUARD_CONFIG_PATH,
    # The management-SSH trust: a swapped CA or an added principal is exactly
    # the tampering this check exists for.
    SSH_CA_PUBLIC_KEY_PATH,
    SSH_CA_SSHD_DROP_IN_PATH,
    ssh_ca_principals_file_path(SSH_CA_BOX_BOOTSTRAP_USER),
    ssh_ca_principals_file_path(GEN2_SLICE_SERVICE_USER),
    BOX_TELEMETRY_SCRIPT_PATH,
    BOX_TELEMETRY_SERVICE_PATH,
    BOX_TELEMETRY_TIMER_PATH,
)

# Collection cadence. Matches the otelcol hostmetrics interval so the counter
# series and the interface series line up.
_COLLECTION_INTERVAL_SECONDS: Final[int] = 60

# Alert thresholds (alert-first: these fire signals, never enforcement).
# Half the enforced 300/s ceiling: sustained operation up here is abnormal
# long before the kernel starts dropping new connections.
NEW_CONNECTIONS_PER_SECOND_SIGNAL_THRESHOLD: Final[int] = GEN2_MAX_NEW_CONNECTIONS_PER_SECOND // 2
# A single VM sustaining more than half the box's declared uplink for a whole
# interval is far outside any observed legitimate workload.
EGRESS_UPLINK_SHARE_SIGNAL_THRESHOLD: Final[float] = 0.5
# Conntrack exhaustion silently breaks every tenant's connectivity; alert with
# headroom left.
CONNTRACK_USE_SHARE_SIGNAL_THRESHOLD: Final[float] = 0.8

# The management bootstrap user (passwordless sudo by design); its sudo
# invocations and SSH logins are the expected operator traffic.
_MANAGEMENT_BOOTSTRAP_USER: Final[str] = SSH_CA_BOX_BOOTSTRAP_USER

# The slice service user's entire expected sudo surface: the same verb set and unit names
# the sudoers grants are rendered from (``render_slice_sudoers``), so the
# allowlist -- ordinal bound included -- can never drift from the grants
# (a denied attempt on a non-granted ordinal must still signal).
_SUDO_ALLOWED_COMMAND_PATTERN: Final[str] = (
    "/usr/bin/systemctl ("
    + "|".join(GEN2_SLICE_SUDO_VERBS)
    + ") ("
    + "|".join(slice_unit_name(ordinal) for ordinal in range(GEN2_MAX_SLICE_COUNT))
    + ")"
)


@pure
def _build_collector_config(
    *,
    overlay_cidr: str,
    declared_uplink_mbps: int,
    management_proxy_static_ips: Sequence[str],
) -> dict[str, object]:
    return {
        "nft_table": GEN2_NFT_TABLE,
        "by_ordinal_dir": GEN2_BY_ORDINAL_DIR,
        "state_dir": BOX_TELEMETRY_STATE_DIR,
        "manifest_path": PREP_ARTIFACT_MANIFEST_PATH,
        # The procfs root (conntrack gauges, process table). Overridable so
        # tests can run the rendered script against a fabricated tree.
        "proc_root": "/proc",
        "signal_marker": BOX_SIGNAL_MARKER,
        "declared_uplink_mbps": declared_uplink_mbps,
        "proxy_static_ips": [str(ip) for ip in management_proxy_static_ips],
        "wireguard_overlay_cidr": overlay_cidr,
        "slice_service_user": GEN2_SLICE_SERVICE_USER,
        "sudo_allowed_command_pattern": _SUDO_ALLOWED_COMMAND_PATTERN,
        "management_bootstrap_user": _MANAGEMENT_BOOTSTRAP_USER,
        "new_connections_per_second_threshold": NEW_CONNECTIONS_PER_SECOND_SIGNAL_THRESHOLD,
        "egress_uplink_share_threshold": EGRESS_UPLINK_SHARE_SIGNAL_THRESHOLD,
        "conntrack_use_share_threshold": CONNTRACK_USE_SHARE_SIGNAL_THRESHOLD,
        "signal_names": {signal.name: str(signal) for signal in BoxTelemetrySignal},
    }


# The collector script body. A plain (non-f) template: the script is python
# itself, so brace interpolation would be unreadable -- the one render-time
# input is the JSON config spliced over the token below.
_COLLECTOR_CONFIG_TOKEN: Final[str] = "__MNGR_TELEMETRY_CONFIG_JSON__"

_COLLECTOR_SCRIPT_TEMPLATE: Final[str] = '''\
#!/usr/bin/env python3
# Managed by mngr (gen-2 box telemetry; specs/slice-fleet-gen2). Runs from
# mngr-box-telemetry.timer. Every stdout line lands in journald under the
# mngr-box-telemetry identifier and ships to the tier's OpenObserve via the
# box collector's journald pipeline. Lines beginning with MNGR_BOX_SIGNAL are
# matched verbatim by the tier's OpenObserve alert rules.
import hashlib
import ipaddress
import json
import os
import pwd
import re
import subprocess
import time

# The raw-string literal keeps every backslash json.dumps emitted (\\", \\\\,
# \\uXXXX escapes inside string values) intact for json.loads.
CONFIG = json.loads(r"""__MNGR_TELEMETRY_CONFIG_JSON__""")

STATE_PATH = os.path.join(CONFIG["state_dir"], "state.json")
CURSOR_PATH = os.path.join(CONFIG["state_dir"], "journal.cursor")
COUNTER_KINDS = ("egress", "ingress", "new_connections", "smtp_blocked")


class CollectorSectionError(Exception):
    """A collection section could not produce its data (reported, never fatal)."""


def emit(event_type, payload):
    line = dict(payload)
    line["mngr_event"] = event_type
    print(json.dumps(line, sort_keys=True), flush=True)


def emit_signal(signal_key, payload):
    signal_name = CONFIG["signal_names"][signal_key]
    line = dict(payload)
    line["mngr_event"] = "signal"
    line["signal"] = signal_name
    print(CONFIG["signal_marker"] + " " + signal_name + " " + json.dumps(line, sort_keys=True), flush=True)


def guarded(section_name, section_function, *args):
    # One broken section must not silence the others; the error line itself
    # ships to OpenObserve, so failures are visible rather than swallowed.
    try:
        return section_function(*args)
    except Exception as exc:
        emit("collector_error", {"section": section_name, "error": str(exc)})
        return None


def run_command(command):
    return subprocess.run(command, capture_output=True, text=True, timeout=30)


def load_state():
    # Only a missing file is the expected first-run case; a corrupted state
    # file propagates to the section guard so the reset is visible as a
    # collector_error (main still falls back to empty state either way).
    try:
        with open(STATE_PATH) as state_file:
            return json.load(state_file)
    except FileNotFoundError:
        return {}


def save_state(state):
    temp_path = STATE_PATH + ".tmp"
    with open(temp_path, "w") as state_file:
        json.dump(state, state_file)
    os.replace(temp_path, STATE_PATH)


def read_instance_name(ordinal):
    env_path = os.path.join(CONFIG["by_ordinal_dir"], str(ordinal), "env")
    try:
        with open(env_path) as env_file:
            for line in env_file:
                if line.startswith("MNGR_SLICE_INSTANCE="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def counter_delta(current, previous):
    # A slice teardown/re-carve recreates its counters at zero; treat a
    # decrease as a reset so a fresh counter's full value is the delta.
    if previous is None:
        return None
    delta = current - previous
    return current if delta < 0 else delta


def collect_slice_counters(state, now):
    result = run_command(["nft", "-j", "list", "counters"])
    if result.returncode != 0:
        raise CollectorSectionError("nft -j list counters failed: " + result.stderr.strip())
    values_by_ordinal = {}
    for entry in json.loads(result.stdout).get("nftables", []):
        counter = entry.get("counter")
        if not isinstance(counter, dict) or counter.get("table") != CONFIG["nft_table"]:
            continue
        match = re.fullmatch(r"slice_(\\d+)_(egress|ingress|new_connections|smtp_blocked)", counter.get("name", ""))
        if match is None:
            continue
        ordinal_values = values_by_ordinal.setdefault(int(match.group(1)), {})
        ordinal_values[match.group(2)] = {
            "bytes": int(counter.get("bytes", 0)),
            "packets": int(counter.get("packets", 0)),
        }

    previous_by_ordinal = state.get("slice_counters", {})
    previous_timestamp = state.get("timestamp")
    interval_seconds = (now - previous_timestamp) if previous_timestamp else None
    for ordinal in sorted(values_by_ordinal):
        kinds = values_by_ordinal[ordinal]
        previous_kinds = previous_by_ordinal.get(str(ordinal), {})
        instance = read_instance_name(ordinal)
        event = {"ordinal": ordinal, "instance": instance}
        deltas = {}
        for kind in COUNTER_KINDS:
            values = kinds.get(kind, {"bytes": 0, "packets": 0})
            event[kind + "_bytes"] = values["bytes"]
            event[kind + "_packets"] = values["packets"]
            previous_values = previous_kinds.get(kind)
            deltas[kind] = {
                "bytes": counter_delta(values["bytes"], previous_values["bytes"] if previous_values else None),
                "packets": counter_delta(values["packets"], previous_values["packets"] if previous_values else None),
            }
            event[kind + "_bytes_delta"] = deltas[kind]["bytes"]
            event[kind + "_packets_delta"] = deltas[kind]["packets"]
        if interval_seconds is not None:
            event["interval_seconds"] = round(interval_seconds, 1)
        emit("slice_counters", event)

        identity = {"ordinal": ordinal, "instance": instance}
        smtp_blocked_delta = deltas["smtp_blocked"]["packets"]
        if smtp_blocked_delta:
            emit_signal("SMTP_BLOCKED", dict(identity, blocked_attempts=smtp_blocked_delta))
        if interval_seconds and interval_seconds > 0:
            new_connections_delta = deltas["new_connections"]["packets"]
            if new_connections_delta is not None:
                rate = new_connections_delta / interval_seconds
                if rate > CONFIG["new_connections_per_second_threshold"]:
                    emit_signal("NEW_CONNECTION_RATE", dict(identity, new_connections_per_second=round(rate, 1)))
            egress_bytes_delta = deltas["egress"]["bytes"]
            uplink_mbps = CONFIG["declared_uplink_mbps"]
            if egress_bytes_delta is not None:
                egress_mbps = egress_bytes_delta * 8 / interval_seconds / 1000000.0
                if egress_mbps > CONFIG["egress_uplink_share_threshold"] * uplink_mbps:
                    emit_signal("EGRESS_RATE", dict(identity, egress_mbps=round(egress_mbps, 1)))

    # The snapshot and the timestamp that dates it advance together: if this
    # section fails, both stay put, so the next successful run divides its
    # multi-interval deltas by the matching multi-interval elapsed time.
    state["slice_counters"] = {
        str(ordinal): kinds for ordinal, kinds in values_by_ordinal.items()
    }
    state["timestamp"] = now


def collect_conntrack():
    netfilter_dir = os.path.join(CONFIG["proc_root"], "sys", "net", "netfilter")
    try:
        with open(os.path.join(netfilter_dir, "nf_conntrack_count")) as count_file:
            count = int(count_file.read().strip())
        with open(os.path.join(netfilter_dir, "nf_conntrack_max")) as max_file:
            maximum = int(max_file.read().strip())
    except FileNotFoundError:
        # The conntrack module is not loaded (no VM has run yet); nothing to
        # report and nothing wrong.
        return
    emit("conntrack", {"count": count, "max": maximum})
    if maximum > 0 and count / maximum > CONFIG["conntrack_use_share_threshold"]:
        emit_signal("CONNTRACK_PRESSURE", {"count": count, "max": maximum})


def uplink_interface():
    result = run_command(["ip", "route"])
    if result.returncode != 0:
        raise CollectorSectionError("ip route failed: " + result.stderr.strip())
    for line in result.stdout.splitlines():
        if line.startswith("default "):
            parts = line.split()
            if "dev" in parts:
                return parts[parts.index("dev") + 1]
    return None


def collect_link_speed():
    interface = uplink_interface()
    if interface is None:
        raise CollectorSectionError("no default route interface found")
    result = run_command(["ethtool", interface])
    if result.returncode != 0:
        # A failed ethtool must not read as "audit passed": raising ships a
        # collector_error, distinct from a clean run whose driver just does
        # not report a numeric speed (which stays a null-field event below).
        raise CollectorSectionError("ethtool " + interface + " failed: " + result.stderr.strip())
    match = re.search(r"Speed:\\s*(\\d+)Mb/s", result.stdout)
    negotiated_mbps = int(match.group(1)) if match else None
    declared_mbps = CONFIG["declared_uplink_mbps"]
    event = {"interface": interface, "declared_mbps": declared_mbps, "negotiated_mbps": negotiated_mbps}
    emit("link_speed_audit", event)
    # Only an UNDER-delivering link signals: the fair-share HTB classes are
    # sized from the declared rate, so a slower physical link silently
    # oversubscribes every tenant. A faster one is harmless and common (e.g.
    # a 10G NIC behind a 1G committed plan rate).
    if negotiated_mbps is not None and negotiated_mbps < declared_mbps:
        emit_signal("LINK_SPEED_MISMATCH", event)


def collect_artifact_integrity():
    drifted_paths = []
    checked_count = 0
    try:
        with open(CONFIG["manifest_path"]) as manifest_file:
            manifest_lines = manifest_file.read().splitlines()
    except FileNotFoundError:
        emit("prep_artifact_integrity", {"checked": 0, "drifted_paths": [], "is_manifest_missing": True})
        emit_signal("PREP_ARTIFACT_DRIFT", {"reason": "manifest_missing"})
        return
    for line in manifest_lines:
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        expected_hash, path = parts[0], parts[1].strip()
        checked_count += 1
        try:
            with open(path, "rb") as artifact_file:
                actual_hash = hashlib.sha256(artifact_file.read()).hexdigest()
        except OSError:
            drifted_paths.append(path)
            continue
        if actual_hash != expected_hash:
            drifted_paths.append(path)
    emit("prep_artifact_integrity", {"checked": checked_count, "drifted_paths": drifted_paths})
    if drifted_paths:
        emit_signal("PREP_ARTIFACT_DRIFT", {"drifted_paths": drifted_paths})


def read_process_table():
    processes = {}
    for entry in os.listdir(CONFIG["proc_root"]):
        if not entry.isdigit():
            continue
        pid = int(entry)
        process_dir = os.path.join(CONFIG["proc_root"], entry)
        try:
            with open(os.path.join(process_dir, "stat")) as stat_file:
                stat_text = stat_file.read()
            owner_uid = os.stat(process_dir).st_uid
        except OSError:
            continue
        # comm may contain spaces/parens; fields resume after the LAST ')'.
        comm = stat_text[stat_text.index("(") + 1 : stat_text.rindex(")")]
        fields = stat_text[stat_text.rindex(")") + 2 :].split()
        processes[pid] = {"comm": comm, "ppid": int(fields[1]), "uid": owner_uid}
    return processes


def collect_qemu_children():
    processes = read_process_table()
    slice_user_pattern = re.compile(r"mngr-slice-\\d+$")
    qemu_pids = set()
    for pid, info in processes.items():
        if not info["comm"].startswith("qemu-system"):
            continue
        try:
            owner = pwd.getpwuid(info["uid"]).pw_name
        except KeyError:
            continue
        if slice_user_pattern.fullmatch(owner):
            qemu_pids.add(pid)
    children = [
        {"pid": pid, "comm": info["comm"], "qemu_pid": info["ppid"]}
        for pid, info in processes.items()
        if info["ppid"] in qemu_pids
    ]
    emit("qemu_children", {"qemu_process_count": len(qemu_pids), "child_count": len(children), "children": children})
    if children:
        emit_signal("QEMU_CHILD_PROCESSES", {"child_count": len(children), "children": children})


def is_allowed_management_source(source):
    try:
        address = ipaddress.ip_address(source)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    if str(address) in CONFIG["proxy_static_ips"]:
        return True
    try:
        return address in ipaddress.ip_network(CONFIG["wireguard_overlay_cidr"])
    except (TypeError, ValueError):
        return False


def scan_journal(anomalies_by_reason, sudo_anomalies, oom_killed_units, static_key_logins):
    base_command = [
        "journalctl", "-o", "json", "--no-pager", "-q",
        "-t", "sshd", "-t", "sshd-session", "-t", "sudo", "-t", "systemd",
    ]
    cursor = None
    try:
        with open(CURSOR_PATH) as cursor_file:
            cursor = cursor_file.read().strip() or None
    except FileNotFoundError:
        pass
    if cursor:
        result = run_command(base_command + ["--after-cursor", cursor])
        if result.returncode != 0:
            # A rotated-away cursor makes journalctl fail; fall back to a
            # bounded window rather than silently skipping the scan.
            result = run_command(base_command + ["--since", "-10 minutes"])
    else:
        result = run_command(base_command + ["--since", "-10 minutes"])
    if result.returncode != 0:
        raise CollectorSectionError("journalctl failed: " + result.stderr.strip())

    last_cursor = cursor
    is_lockdown_active = bool(CONFIG["proxy_static_ips"])
    for line in result.stdout.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        last_cursor = entry.get("__CURSOR", last_cursor)
        identifier = entry.get("SYSLOG_IDENTIFIER", "")
        message = entry.get("MESSAGE", "")
        if not isinstance(message, str):
            continue
        if identifier in ("sshd", "sshd-session"):
            accepted = re.search(r"Accepted (\\S+) for (\\S+) from (\\S+) port \\d+ ssh2(.*)$", message)
            if accepted:
                method, user, source, auth_detail = accepted.groups()
                # A certificate login logs `ID <key id> (serial N) CA ...`; the key id
                # is the requester Vault recorded, so every login is attributable.
                certificate = re.search(r" ID (\\S+) \\(serial (\\d+)\\) CA ", auth_detail)
                key_id = certificate.group(1) if certificate else None
                if certificate is None and method == "publickey":
                    # Gen-2 management SSH is by certificate only; a raw key login
                    # means a static key is authorized somewhere on this box.
                    static_key_logins.append({"user": user, "source": source})
                emit(
                    "management_login",
                    {"user": user, "source": source, "method": method, "key_id": key_id,
                     "serial": int(certificate.group(2)) if certificate else None},
                )
                if user == "root":
                    reason = "direct_root_login"
                elif method == "password":
                    reason = "password_login"
                elif is_lockdown_active and not is_allowed_management_source(source):
                    reason = "login_outside_allowlist"
                else:
                    continue
                anomaly = anomalies_by_reason.setdefault(reason, {"count": 0, "sample": None})
                anomaly["count"] += 1
                if anomaly["sample"] is None:
                    anomaly["sample"] = {"user": user, "source": source, "method": method}
            elif is_lockdown_active and re.search(r"Failed password|Invalid user", message):
                # Pre-lockdown this is background internet scan noise; behind
                # the lockdown NOTHING should even reach password auth.
                anomaly = anomalies_by_reason.setdefault("password_attempt", {"count": 0, "sample": None})
                anomaly["count"] += 1
        elif identifier == "sudo":
            invocation = re.match(r"\\s*(\\S+) : .*COMMAND=(.*)$", message)
            if invocation:
                user, command = invocation.groups()
                if user in (CONFIG["management_bootstrap_user"], "root"):
                    continue
                if user == CONFIG["slice_service_user"] and re.fullmatch(
                    CONFIG["sudo_allowed_command_pattern"], command
                ):
                    continue
                sudo_anomalies.append({"user": user, "command": command})
            elif "NOT in sudoers" in message:
                sudo_anomalies.append({"denied": message.strip()[:200]})
        elif identifier == "systemd":
            # systemd (PID 1) prefixes a unit's messages with the unit name;
            # an OOM kill of a slice VM's unit logs the kill itself and the
            # unit's failure result, either of which identifies the event.
            oom_kill = re.match(
                r"(mngr-slice@\\d+\\.service): (A process of this unit has been killed by the OOM killer|Failed with result 'oom-kill')",
                message,
            )
            if oom_kill:
                oom_killed_units.setdefault(oom_kill.group(1), 0)
                oom_killed_units[oom_kill.group(1)] += 1

    if last_cursor and last_cursor != cursor:
        temp_path = CURSOR_PATH + ".tmp"
        with open(temp_path, "w") as cursor_file:
            cursor_file.write(last_cursor)
        os.replace(temp_path, CURSOR_PATH)


def collect_management_plane_signals():
    anomalies_by_reason = {}
    sudo_anomalies = []
    oom_killed_units = {}
    static_key_logins = []
    scan_journal(anomalies_by_reason, sudo_anomalies, oom_killed_units, static_key_logins)
    if static_key_logins:
        anomalies_by_reason["static_key_login"] = {"count": len(static_key_logins), "sample": static_key_logins[0]}
    # Aggregated: one signal line per reason per run, however many raw journal
    # lines matched (an SSH flood must not become a signal flood).
    for reason in sorted(anomalies_by_reason):
        anomaly = anomalies_by_reason[reason]
        emit_signal("MANAGEMENT_SSH_ANOMALY", {"reason": reason, "count": anomaly["count"], "sample": anomaly["sample"]})
    if sudo_anomalies:
        emit_signal("SUDO_ANOMALY", {"count": len(sudo_anomalies), "sample": sudo_anomalies[0]})
    if oom_killed_units:
        units = sorted(oom_killed_units)
        emit_signal("SLICE_UNIT_OOM_KILLED", {"units": units, "count": len(units)})


def main():
    os.makedirs(CONFIG["state_dir"], exist_ok=True)
    state = guarded("state_load", load_state)
    if state is None:
        state = {}
    now = time.time()
    guarded("slice_counters", collect_slice_counters, state, now)
    guarded("conntrack", collect_conntrack)
    guarded("link_speed", collect_link_speed)
    guarded("prep_artifact_integrity", collect_artifact_integrity)
    guarded("qemu_children", collect_qemu_children)
    guarded("management_plane", collect_management_plane_signals)
    guarded("state_save", save_state, state)


if __name__ == "__main__":
    main()
'''


@pure
def _render_collector_script_from_config(config: Mapping[str, object]) -> str:
    return _COLLECTOR_SCRIPT_TEMPLATE.replace(_COLLECTOR_CONFIG_TOKEN, json.dumps(config, sort_keys=True))


@pure
def render_box_telemetry_collector_script(
    *,
    overlay_cidr: str,
    declared_uplink_mbps: int,
    management_proxy_static_ips: Sequence[str],
) -> str:
    """Render the collector script with the box's render-time config spliced in."""
    config = _build_collector_config(
        overlay_cidr=overlay_cidr,
        declared_uplink_mbps=declared_uplink_mbps,
        management_proxy_static_ips=management_proxy_static_ips,
    )
    return _render_collector_script_from_config(config)


@pure
def render_box_telemetry_service_unit() -> str:
    return f"""\
[Unit]
Description=mngr gen-2 box telemetry collector

[Service]
Type=oneshot
ExecStart={BOX_TELEMETRY_SCRIPT_PATH}
SyslogIdentifier={BOX_TELEMETRY_SYSLOG_IDENTIFIER}
"""


@pure
def render_box_telemetry_timer_unit() -> str:
    return f"""\
[Unit]
Description=Run the mngr gen-2 box telemetry collector every collection interval

[Timer]
OnBootSec=2min
OnUnitActiveSec={_COLLECTION_INTERVAL_SECONDS}s
AccuracySec=10s

[Install]
WantedBy=timers.target
"""


@pure
def render_box_telemetry_prep_section(
    *,
    overlay_cidr: str,
    declared_uplink_mbps: int,
    management_proxy_static_ips: Sequence[str],
) -> str:
    """The idempotent root bash section installing the collector, its units, and the artifact manifest.

    Content-converged like the other gen-2 prep artifacts; must run AFTER the
    prep artifacts and the WireGuard config are installed, because it records
    their installed hashes as the integrity check's expectation.
    """
    collector_script = render_box_telemetry_collector_script(
        overlay_cidr=overlay_cidr,
        declared_uplink_mbps=declared_uplink_mbps,
        management_proxy_static_ips=management_proxy_static_ips,
    )
    service_unit = render_box_telemetry_service_unit()
    timer_unit = render_box_telemetry_timer_unit()
    manifest_targets = " ".join(PREP_ARTIFACT_MANIFEST_TARGETS)
    return f"""\
# Box telemetry: the tier-1 collector (nft counters, link-speed audit,
# artifact integrity, qemu-children tripwire, auth-log signals) on a systemd
# timer, emitting journald lines the otelcol pipeline ships.
# Root-installed files are staged inside the root-only state dir (never a
# predictable /tmp name an unprivileged local user could pre-create and race).
install -d -m 700 {BOX_TELEMETRY_STATE_DIR}
cat > {BOX_TELEMETRY_STATE_DIR}/script.mngr-tmp <<'MNGR_BOX_TELEMETRY_SCRIPT'
{collector_script}\
MNGR_BOX_TELEMETRY_SCRIPT
if ! cmp -s {BOX_TELEMETRY_STATE_DIR}/script.mngr-tmp {BOX_TELEMETRY_SCRIPT_PATH}; then
    install -m 755 {BOX_TELEMETRY_STATE_DIR}/script.mngr-tmp {BOX_TELEMETRY_SCRIPT_PATH}
fi
rm -f {BOX_TELEMETRY_STATE_DIR}/script.mngr-tmp

cat > {BOX_TELEMETRY_STATE_DIR}/service.mngr-tmp <<'MNGR_BOX_TELEMETRY_SERVICE'
{service_unit}\
MNGR_BOX_TELEMETRY_SERVICE
cat > {BOX_TELEMETRY_STATE_DIR}/timer.mngr-tmp <<'MNGR_BOX_TELEMETRY_TIMER'
{timer_unit}\
MNGR_BOX_TELEMETRY_TIMER
if ! cmp -s {BOX_TELEMETRY_STATE_DIR}/service.mngr-tmp {BOX_TELEMETRY_SERVICE_PATH} \\
    || ! cmp -s {BOX_TELEMETRY_STATE_DIR}/timer.mngr-tmp {BOX_TELEMETRY_TIMER_PATH}; then
    install -m 644 {BOX_TELEMETRY_STATE_DIR}/service.mngr-tmp {BOX_TELEMETRY_SERVICE_PATH}
    install -m 644 {BOX_TELEMETRY_STATE_DIR}/timer.mngr-tmp {BOX_TELEMETRY_TIMER_PATH}
    systemctl daemon-reload
fi
rm -f {BOX_TELEMETRY_STATE_DIR}/service.mngr-tmp {BOX_TELEMETRY_STATE_DIR}/timer.mngr-tmp
systemctl enable mngr-box-telemetry.timer
systemctl start mngr-box-telemetry.timer

# Record the installed prep artifacts' hashes as the integrity check's
# expectation. Runs last so it captures exactly what this prep converged
# (wg0.conf includes the on-box private key, so the manifest stays root-only).
sha256sum {manifest_targets} > {PREP_ARTIFACT_MANIFEST_PATH}.mngr-tmp
install -m 600 {PREP_ARTIFACT_MANIFEST_PATH}.mngr-tmp {PREP_ARTIFACT_MANIFEST_PATH}
rm -f {PREP_ARTIFACT_MANIFEST_PATH}.mngr-tmp
"""
