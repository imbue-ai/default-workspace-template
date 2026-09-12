import json
import os
import subprocess
import sys
import time
from hashlib import sha256
from pathlib import Path

from imbue.minds_admin.slices.box_telemetry import BOX_TELEMETRY_SCRIPT_PATH
from imbue.minds_admin.slices.box_telemetry import BOX_TELEMETRY_SYSLOG_IDENTIFIER
from imbue.minds_admin.slices.box_telemetry import PREP_ARTIFACT_MANIFEST_PATH
from imbue.minds_admin.slices.box_telemetry import PREP_ARTIFACT_MANIFEST_TARGETS
from imbue.minds_admin.slices.box_telemetry import _build_collector_config
from imbue.minds_admin.slices.box_telemetry import _render_collector_script_from_config
from imbue.minds_admin.slices.box_telemetry import render_box_telemetry_collector_script
from imbue.minds_admin.slices.box_telemetry import render_box_telemetry_prep_section
from imbue.minds_admin.slices.box_telemetry import render_box_telemetry_service_unit
from imbue.minds_admin.slices.box_telemetry import render_box_telemetry_timer_unit


def test_collector_script_is_valid_python_and_carries_the_config() -> None:
    script = render_box_telemetry_collector_script(
        overlay_cidr="10.112.0.0/16", declared_uplink_mbps=1000, management_proxy_static_ips=("203.0.113.5",)
    )
    compile(script, "mngr-box-telemetry", "exec")
    assert '"declared_uplink_mbps": 1000' in script
    assert '"203.0.113.5"' in script
    assert "MNGR_BOX_SIGNAL" in script


def test_service_unit_pins_the_syslog_identifier_the_alert_rules_depend_on() -> None:
    unit = render_box_telemetry_service_unit()
    assert f"SyslogIdentifier={BOX_TELEMETRY_SYSLOG_IDENTIFIER}" in unit
    assert f"ExecStart={BOX_TELEMETRY_SCRIPT_PATH}" in unit


def test_timer_unit_runs_on_a_short_interval_and_installs_at_boot() -> None:
    timer = render_box_telemetry_timer_unit()
    assert "OnUnitActiveSec=60s" in timer
    assert "WantedBy=timers.target" in timer


def test_manifest_covers_the_storage_volume_artifacts() -> None:
    # A change to the crypttab entry or to the bind-mount units would silently
    # move the journal, the service user's home or the temp dirs back onto the
    # plain root partition, so the integrity check must cover them.
    assert "/etc/crypttab" in PREP_ARTIFACT_MANIFEST_TARGETS
    for unit_path in (
        "/etc/systemd/system/var-log-journal.mount",
        "/etc/systemd/system/home-slicehost.mount",
        "/etc/systemd/system/tmp.mount",
        "/etc/systemd/system/var-tmp.mount",
        "/etc/systemd/system/systemd-journal-flush.service.d/mngr-storage.conf",
    ):
        assert unit_path in PREP_ARTIFACT_MANIFEST_TARGETS


def test_prep_section_converges_artifacts_and_records_the_hash_manifest() -> None:
    section = render_box_telemetry_prep_section(
        overlay_cidr="10.112.0.0/16", declared_uplink_mbps=500, management_proxy_static_ips=()
    )
    # Content-converged install of the script and both units, then the timer
    # enabled, then the artifact hash manifest recorded root-only.
    assert section.count("cmp -s") == 3
    assert "systemctl enable mngr-box-telemetry.timer" in section
    assert "systemctl start mngr-box-telemetry.timer" in section
    for target in PREP_ARTIFACT_MANIFEST_TARGETS:
        assert target in section
    assert f"install -m 600 {PREP_ARTIFACT_MANIFEST_PATH}.mngr-tmp {PREP_ARTIFACT_MANIFEST_PATH}" in section


def _write_stub(bin_dir: Path, name: str, output: str) -> None:
    stub_path = bin_dir / name
    stub_path.write_text(f"#!/bin/sh\ncat <<'MNGR_STUB_EOF'\n{output}\nMNGR_STUB_EOF\n")
    stub_path.chmod(0o755)


def _write_fake_proc(tmp_path: Path, *, conntrack_count: int, conntrack_max: int) -> Path:
    """A fabricated procfs root: the conntrack gauges plus an empty process table."""
    proc_root = tmp_path / "proc"
    netfilter_dir = proc_root / "sys" / "net" / "netfilter"
    netfilter_dir.mkdir(parents=True)
    (netfilter_dir / "nf_conntrack_count").write_text(f"{conntrack_count}\n")
    (netfilter_dir / "nf_conntrack_max").write_text(f"{conntrack_max}\n")
    return proc_root


def _collector_config_against(tmp_path: Path) -> dict[str, object]:
    """The rendered config pointed at the test's fabricated dirs (which must already exist)."""
    config = _build_collector_config(
        overlay_cidr="10.112.0.0/16", declared_uplink_mbps=1000, management_proxy_static_ips=["203.0.113.5"]
    )
    config["state_dir"] = str(tmp_path / "state")
    config["by_ordinal_dir"] = str(tmp_path / "by-ordinal")
    config["manifest_path"] = str(tmp_path / "manifest.sha256")
    config["proc_root"] = str(tmp_path / "proc")
    return config


def _run_rendered_collector(tmp_path: Path, config: dict[str, object]) -> subprocess.CompletedProcess[str]:
    script_path = tmp_path / "collector.py"
    script_path.write_text(_render_collector_script_from_config(config))
    return subprocess.run(
        [sys.executable, str(script_path)],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}"},
    )


def _nft_counters_json(*, egress_bytes: int, new_connections_packets: int, smtp_blocked_packets: int) -> str:
    counters = []
    for name, packets, byte_count in (
        ("slice_3_egress", 1000, egress_bytes),
        ("slice_3_ingress", 500, 12345),
        ("slice_3_new_connections", new_connections_packets, 0),
        ("slice_3_smtp_blocked", smtp_blocked_packets, smtp_blocked_packets * 60),
    ):
        counters.append(
            {
                "counter": {
                    "family": "inet",
                    "table": "mngr_slices",
                    "name": name,
                    "packets": packets,
                    "bytes": byte_count,
                }
            }
        )
    # An unrelated table's counter must be ignored.
    counters.append(
        {"counter": {"family": "inet", "table": "other", "name": "slice_9_egress", "packets": 1, "bytes": 1}}
    )
    return json.dumps({"nftables": [{"metainfo": {}}] + counters})


def _journal_lines() -> str:
    entries = [
        # Direct root login: always an anomaly.
        {
            "SYSLOG_IDENTIFIER": "sshd",
            "MESSAGE": "Accepted publickey for root from 8.8.8.8 port 1 ssh2",
            "__CURSOR": "c1",
        },
        # An operator certificate login from an allowlisted proxy IP: expected,
        # and attributable through the key id Vault stamped.
        {
            "SYSLOG_IDENTIFIER": "sshd-session",
            "MESSAGE": (
                "Accepted publickey for debian from 203.0.113.5 port 2 ssh2: ED25519-CERT SHA256:abc "
                "ID operator:oidc-josh@imbue.com (serial 7) CA ED25519 SHA256:def"
            ),
            "__CURSOR": "c2",
        },
        # slicehost logging in with a raw key: no static key belongs on a gen-2
        # box, so this is an anomaly even from an allowlisted source.
        {
            "SYSLOG_IDENTIFIER": "sshd-session",
            "MESSAGE": "Accepted publickey for slicehost from 203.0.113.5 port 3 ssh2: ED25519 SHA256:raw",
            "__CURSOR": "c2b",
        },
        # A password probe reaching sshd behind the lockdown.
        {
            "SYSLOG_IDENTIFIER": "sshd",
            "MESSAGE": "Failed password for invalid user admin from 5.6.7.8",
            "__CURSOR": "c3",
        },
        # slicehost driving a slice unit: exactly the sudoers grant.
        {
            "SYSLOG_IDENTIFIER": "sudo",
            "MESSAGE": "slicehost : TTY=unknown ; PWD=/home/slicehost ; USER=root ; COMMAND=/usr/bin/systemctl start mngr-slice@3",
            "__CURSOR": "c4",
        },
        # slicehost outside the grants: an anomaly.
        {
            "SYSLOG_IDENTIFIER": "sudo",
            "MESSAGE": "slicehost : TTY=unknown ; PWD=/home/slicehost ; USER=root ; COMMAND=/usr/bin/cat /etc/shadow",
            "__CURSOR": "c5",
        },
        # slicehost using a granted verb on a NON-granted ordinal (sudoers
        # denies it, logging the same COMMAND= shape): still an anomaly.
        {
            "SYSLOG_IDENTIFIER": "sudo",
            "MESSAGE": "slicehost : command not allowed ; TTY=unknown ; PWD=/home/slicehost ; USER=root ; COMMAND=/usr/bin/systemctl start mngr-slice@999",
            "__CURSOR": "c6",
        },
        # systemd OOM-killing a slice VM's unit (both lines it logs for one
        # kill), plus an unrelated unit's OOM kill that must not count.
        {
            "SYSLOG_IDENTIFIER": "systemd",
            "MESSAGE": "mngr-slice@3.service: A process of this unit has been killed by the OOM killer.",
            "__CURSOR": "c7",
        },
        {
            "SYSLOG_IDENTIFIER": "systemd",
            "MESSAGE": "mngr-slice@3.service: Failed with result 'oom-kill'.",
            "__CURSOR": "c8",
        },
        {
            "SYSLOG_IDENTIFIER": "systemd",
            "MESSAGE": "some-other.service: Failed with result 'oom-kill'.",
            "__CURSOR": "c9",
        },
    ]
    return "\n".join(json.dumps(entry) for entry in entries)


def test_collector_run_emits_counters_and_signals_against_stubbed_box_commands(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    by_ordinal_dir = tmp_path / "by-ordinal"
    (by_ordinal_dir / "3").mkdir(parents=True)
    (by_ordinal_dir / "3" / "env").write_text("MNGR_SLICE_ORDINAL=3\nMNGR_SLICE_INSTANCE=host-abc123\n")
    # A conntrack table over the 80% pressure threshold.
    _write_fake_proc(tmp_path, conntrack_count=900, conntrack_max=1000)

    # One intact artifact and one drifted artifact behind the manifest.
    intact_artifact = tmp_path / "unit.service"
    intact_artifact.write_text("unit content")
    drifted_artifact = tmp_path / "helper.sh"
    drifted_artifact.write_text("helper content AFTER drift")
    manifest = tmp_path / "manifest.sha256"
    manifest.write_text(
        f"{sha256(b'unit content').hexdigest()}  {intact_artifact}\n"
        f"{sha256(b'helper content').hexdigest()}  {drifted_artifact}\n"
    )

    _write_stub(
        bin_dir,
        "nft",
        _nft_counters_json(egress_bytes=8_000_000_000, new_connections_packets=60000, smtp_blocked_packets=2),
    )
    _write_stub(bin_dir, "ip", "default via 192.0.2.1 dev eth0\n192.0.2.0/24 dev eth0")
    # Slower than the declared 1000: the under-delivering-link case that signals.
    _write_stub(bin_dir, "ethtool", "Settings for eth0:\n\tSpeed: 100Mb/s\n\tDuplex: Full")
    _write_stub(bin_dir, "journalctl", _journal_lines())
    # The storage root mounted from the plain partition: the never-encrypted case.
    _write_stub(bin_dir, "findmnt", "/dev/md4")

    # Seed the state so the run computes deltas over a known 60s interval,
    # with every previous counter at zero (a first-ever observation of a
    # counter deliberately yields no delta and so no signal).
    zero = {"bytes": 0, "packets": 0}
    previous_counters = {"3": {"egress": zero, "ingress": zero, "new_connections": zero, "smtp_blocked": zero}}
    (state_dir / "state.json").write_text(
        json.dumps({"timestamp": time.time() - 60.0, "slice_counters": previous_counters})
    )

    result = _run_rendered_collector(tmp_path, _collector_config_against(tmp_path))
    assert result.returncode == 0, result.stderr

    lines = result.stdout.splitlines()
    signal_lines = [line for line in lines if line.startswith("MNGR_BOX_SIGNAL ")]
    signals = {line.split()[1] for line in signal_lines}
    assert signals == {
        "SMTP_BLOCKED",
        "NEW_CONNECTION_RATE",
        "EGRESS_RATE",
        "CONNTRACK_PRESSURE",
        "LINK_SPEED_MISMATCH",
        "PREP_ARTIFACT_DRIFT",
        "MANAGEMENT_SSH_ANOMALY",
        "SUDO_ANOMALY",
        "SLICE_UNIT_OOM_KILLED",
        "STORAGE_VOLUME_LOCKED",
    }

    events = [json.loads(line) for line in lines if not line.startswith("MNGR_BOX_SIGNAL ")]
    counter_events = [event for event in events if event["mngr_event"] == "slice_counters"]
    assert len(counter_events) == 1
    assert counter_events[0]["ordinal"] == 3
    assert counter_events[0]["instance"] == "host-abc123"
    assert counter_events[0]["smtp_blocked_packets_delta"] == 2
    # The foreign-table counter must not have produced a second slice.
    assert not any(event.get("ordinal") == 9 for event in counter_events)

    drift_events = [event for event in events if event["mngr_event"] == "prep_artifact_integrity"]
    assert drift_events[0]["drifted_paths"] == [str(drifted_artifact)]

    ssh_signals = [json.loads(line.split(" ", 2)[2]) for line in signal_lines if " MANAGEMENT_SSH_ANOMALY " in line]
    assert {signal["reason"] for signal in ssh_signals} == {
        "direct_root_login",
        "password_attempt",
        "static_key_login",
    }
    static_key_signal = next(signal for signal in ssh_signals if signal["reason"] == "static_key_login")
    # The root login carried no certificate either, so both raw-key logins count.
    assert static_key_signal["count"] == 2
    # Every accepted login is emitted as an attributable event; the certificate
    # login carries the key id Vault stamped for the requester.
    login_events = [event for event in events if event["mngr_event"] == "management_login"]
    debian_login = next(event for event in login_events if event["user"] == "debian")
    assert debian_login["key_id"] == "operator:oidc-josh@imbue.com"
    assert debian_login["serial"] == 7
    assert next(event for event in login_events if event["user"] == "slicehost")["key_id"] is None

    # Both the off-grant command and the granted verb on a non-granted
    # ordinal count; the sample is the first anomaly seen.
    sudo_signals = [json.loads(line.split(" ", 2)[2]) for line in signal_lines if " SUDO_ANOMALY " in line]
    assert sudo_signals[0]["count"] == 2
    assert sudo_signals[0]["sample"]["command"] == "/usr/bin/cat /etc/shadow"

    # One OOM signal per run naming the killed unit once, however many lines
    # systemd logged for the kill; the unrelated unit is not a slice.
    oom_signals = [json.loads(line.split(" ", 2)[2]) for line in signal_lines if " SLICE_UNIT_OOM_KILLED " in line]
    assert oom_signals == [
        {"mngr_event": "signal", "signal": "SLICE_UNIT_OOM_KILLED", "units": ["mngr-slice@3.service"], "count": 1}
    ]

    # The journal cursor advanced to the last stubbed entry.
    assert (state_dir / "journal.cursor").read_text() == "c9"

    storage_signals = [json.loads(line.split(" ", 2)[2]) for line in signal_lines if " STORAGE_VOLUME_LOCKED " in line]
    assert storage_signals == [
        {
            "mngr_event": "signal",
            "signal": "STORAGE_VOLUME_LOCKED",
            "mounted_source": "/dev/md4",
            "is_encrypted": False,
            "reason": "unencrypted",
        }
    ]


def test_collector_signals_a_locked_storage_volume_with_nothing_mounted(tmp_path: Path) -> None:
    bin_dir = _set_up_healthy_box(tmp_path)
    # findmnt exits non-zero when the storage root is not a mount point.
    (bin_dir / "findmnt").write_text("#!/bin/sh\nexit 1\n")

    result = _run_rendered_collector(tmp_path, _collector_config_against(tmp_path))
    assert result.returncode == 0, result.stderr
    signal_lines = [line for line in result.stdout.splitlines() if line.startswith("MNGR_BOX_SIGNAL ")]
    assert [line.split()[1] for line in signal_lines] == ["STORAGE_VOLUME_LOCKED"]
    assert json.loads(signal_lines[0].split(" ", 2)[2])["reason"] == "locked"
    events = [json.loads(line) for line in result.stdout.splitlines() if not line.startswith("MNGR_BOX_SIGNAL ")]
    storage_event = next(event for event in events if event["mngr_event"] == "storage_volume")
    assert storage_event == {"mngr_event": "storage_volume", "mounted_source": None, "is_encrypted": False}


def _set_up_healthy_box(tmp_path: Path) -> Path:
    """Fabricate a box where nothing signals; returns the stub bin dir for per-test overrides."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "state").mkdir()
    (tmp_path / "by-ordinal").mkdir()

    artifact = tmp_path / "unit.service"
    artifact.write_text("unit content")
    manifest = tmp_path / "manifest.sha256"
    manifest.write_text(f"{sha256(b'unit content').hexdigest()}  {artifact}\n")
    # A conntrack table with plenty of headroom.
    _write_fake_proc(tmp_path, conntrack_count=10, conntrack_max=1000)

    _write_stub(bin_dir, "nft", json.dumps({"nftables": [{"metainfo": {}}]}))
    _write_stub(bin_dir, "ip", "default via 192.0.2.1 dev eth0")
    # FASTER than declared is healthy (a 10G NIC behind a 1G plan rate must
    # never signal).
    _write_stub(bin_dir, "ethtool", "Settings for eth0:\n\tSpeed: 10000Mb/s")
    _write_stub(bin_dir, "journalctl", "")
    # The storage root mounted from its LUKS mapper: the encrypted steady state.
    _write_stub(bin_dir, "findmnt", "/dev/mapper/mngr-storage")
    return bin_dir


def test_collector_run_emits_no_signals_when_everything_is_healthy(tmp_path: Path) -> None:
    _set_up_healthy_box(tmp_path)

    result = _run_rendered_collector(tmp_path, _collector_config_against(tmp_path))
    assert result.returncode == 0, result.stderr
    assert not any(line.startswith("MNGR_BOX_SIGNAL ") for line in result.stdout.splitlines())
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert not any(event["mngr_event"] == "collector_error" for event in events)
    assert any(event["mngr_event"] == "link_speed_audit" for event in events)
    assert any(event["mngr_event"] == "conntrack" for event in events)


def test_collector_reports_a_failing_ethtool_instead_of_a_silently_null_audit(tmp_path: Path) -> None:
    bin_dir = _set_up_healthy_box(tmp_path)
    failing_ethtool = bin_dir / "ethtool"
    failing_ethtool.write_text("#!/bin/sh\necho 'Cannot get device settings' >&2\nexit 75\n")
    failing_ethtool.chmod(0o755)

    result = _run_rendered_collector(tmp_path, _collector_config_against(tmp_path))
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert not any(line.startswith("MNGR_BOX_SIGNAL ") for line in lines)
    events = [json.loads(line) for line in lines]
    error_events = [event for event in events if event["mngr_event"] == "collector_error"]
    assert [event["section"] for event in error_events] == ["link_speed"]
    assert "ethtool eth0 failed" in error_events[0]["error"]
    # The failure must not read as a completed (null) audit.
    assert not any(event["mngr_event"] == "link_speed_audit" for event in events)


def test_collector_reports_a_corrupted_state_file_and_still_completes_the_run(tmp_path: Path) -> None:
    _set_up_healthy_box(tmp_path)
    (tmp_path / "state" / "state.json").write_text("{not json")

    result = _run_rendered_collector(tmp_path, _collector_config_against(tmp_path))
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert not any(line.startswith("MNGR_BOX_SIGNAL ") for line in lines)
    events = [json.loads(line) for line in lines]
    # The reset to empty state is visible, and every section still runs.
    error_events = [event for event in events if event["mngr_event"] == "collector_error"]
    assert [event["section"] for event in error_events] == ["state_load"]
    assert any(event["mngr_event"] == "conntrack" for event in events)
    # The next save overwrites the corrupt file with valid state.
    assert "timestamp" in json.loads((tmp_path / "state" / "state.json").read_text())
