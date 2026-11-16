"""Tests for the tray's resource gauge: its settings, the readings, the warning rules, and the monitor."""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from pydantic import Field
from pydantic import ValidationError

from imbue.system_interface.shell.resource_gauge import AGENT_NOTICE_FILENAME
from imbue.system_interface.shell.resource_gauge import AlertSeverity
from imbue.system_interface.shell.resource_gauge import AmountReading
from imbue.system_interface.shell.resource_gauge import CpuReading
from imbue.system_interface.shell.resource_gauge import DEFAULT_RESOURCE_GAUGE_SETTINGS
from imbue.system_interface.shell.resource_gauge import DeliveryRecord
from imbue.system_interface.shell.resource_gauge import DeliveryStore
from imbue.system_interface.shell.resource_gauge import DiskReading
from imbue.system_interface.shell.resource_gauge import GIB
from imbue.system_interface.shell.resource_gauge import GatewayResourceNotifier
from imbue.system_interface.shell.resource_gauge import GaugeResource
from imbue.system_interface.shell.resource_gauge import GaugeVisibility
from imbue.system_interface.shell.resource_gauge import NOTIFY_RETRY_SECONDS
from imbue.system_interface.shell.resource_gauge import NO_READINGS
from imbue.system_interface.shell.resource_gauge import PUSH_HEARTBEAT_SECONDS
from imbue.system_interface.shell.resource_gauge import RESOURCE_GAUGE_FILENAME
from imbue.system_interface.shell.resource_gauge import ResourceGaugeMonitor
from imbue.system_interface.shell.resource_gauge import ResourceGaugeSettings
from imbue.system_interface.shell.resource_gauge import ResourceGaugeSettingsStore
from imbue.system_interface.shell.resource_gauge import ResourceGaugeView
from imbue.system_interface.shell.resource_gauge import ResourceNotifierInterface
from imbue.system_interface.shell.resource_gauge import ResourceReadings
from imbue.system_interface.shell.resource_gauge import StopLedgerReader
from imbue.system_interface.shell.resource_gauge import StoppedProgram
from imbue.system_interface.shell.resource_gauge import describe_agent_notice
from imbue.system_interface.shell.resource_gauge import describe_alert
from imbue.system_interface.shell.resource_gauge import describe_notification
from imbue.system_interface.shell.resource_gauge import describe_stops_notification
from imbue.system_interface.shell.resource_gauge import estimate_seconds_to_full
from imbue.system_interface.shell.resource_gauge import format_duration
from imbue.system_interface.shell.resource_gauge import format_gigabytes
from imbue.system_interface.shell.resource_gauge import next_warnings
from imbue.system_interface.shell.resource_gauge import parse_cpu_reading
from imbue.system_interface.shell.resource_gauge import read_cpu
from imbue.system_interface.shell.testing import message_handling_app
from imbue.system_interface.testing import serve_app

_DISK_TOTAL_GB = 100.0


def _disk(free_gb: float, seconds_to_full: float | None = None) -> DiskReading:
    return DiskReading(
        used_bytes=int((_DISK_TOTAL_GB - free_gb) * GIB),
        free_bytes=int(free_gb * GIB),
        total_bytes=int(_DISK_TOTAL_GB * GIB),
        seconds_to_full=seconds_to_full,
    )


def _memory(percent: float) -> AmountReading:
    """Memory at ``percent`` in use of 8 GiB, in units that divide evenly so the share is exact."""
    unit = 8 * GIB // 1000
    used = round(percent * 10) * unit
    return AmountReading(used_bytes=used, free_bytes=1000 * unit - used, total_bytes=1000 * unit)


def _readings(
    disk_free_gb: float | None = None, memory_percent: float | None = None, seconds_to_full: float | None = None
) -> ResourceReadings:
    return ResourceReadings(
        cpu=None,
        memory=_memory(memory_percent) if memory_percent is not None else None,
        disk=_disk(disk_free_gb, seconds_to_full) if disk_free_gb is not None else None,
    )


class _RecordingNotifier(ResourceNotifierInterface):
    sent: list[tuple[str, GaugeResource]] = Field(default_factory=list, description="The titles sent, and where to")
    is_reachable: bool = Field(default=True, description="Whether a notification is taken")

    def notify(self, title: str, message: str, view: GaugeResource) -> bool:
        if not self.is_reachable:
            return False
        self.sent.append((title, view))
        return True


class _Harness:
    """A monitor over scripted readings, a settable clock, and recorders for what it tells everyone."""

    def __init__(self, tmp_path: Path, has_notifier: bool = True) -> None:
        self.now = 1_000_000.0
        self.readings = NO_READINGS
        self.stops: list[StoppedProgram] = []
        self.pushed: list[ResourceGaugeView] = []
        self.notifier = _RecordingNotifier()
        self.state_directory = tmp_path
        self.monitor = self.build(has_notifier)

    def build(self, has_notifier: bool = True) -> ResourceGaugeMonitor:
        """A monitor over the same state directory: what a restart of the shell makes."""
        return ResourceGaugeMonitor(
            settings_store=ResourceGaugeSettingsStore(state_directory=self.state_directory),
            delivery_store=DeliveryStore(state_directory=self.state_directory),
            read_readings=lambda: self.readings,
            read_stops=lambda: self.stops,
            notifier=self.notifier if has_notifier else None,
            push=self.pushed.append,
            agent_notice_path=self.state_directory / AGENT_NOTICE_FILENAME,
            clock=lambda: self.now,
        )

    def read(self, monitor: ResourceGaugeMonitor | None = None, advance_seconds: float = 5.0, **readings: Any) -> None:
        self.now += advance_seconds
        if readings:
            self.readings = _readings(**readings)
        (monitor or self.monitor).take_reading()

    def titles(self) -> list[str]:
        return [title for title, _ in self.notifier.sent]


def test_settings_default_to_a_disk_warning_at_5_gb_left_and_round_trip(tmp_path: Path) -> None:
    store = ResourceGaugeSettingsStore(state_directory=tmp_path)
    assert store.read() == DEFAULT_RESOURCE_GAUGE_SETTINGS
    assert store.read().visibility == GaugeVisibility.WHEN_WARNING
    assert store.read().disk_warning_free_gb == 5.0
    assert store.read().memory_warning_percent is None
    settings = ResourceGaugeSettings(
        visibility=GaugeVisibility.ALWAYS,
        disk_warning_free_gb=2.5,
        memory_warning_percent=95,
        cpu_warning_percent=None,
    )
    store.write(settings)
    assert store.read() == settings
    assert json.loads((tmp_path / RESOURCE_GAUGE_FILENAME).read_text()) == {
        "version": 1,
        "visibility": "always",
        "disk_warning_free_gb": 2.5,
        "memory_warning_percent": 95,
        "cpu_warning_percent": None,
    }


def test_an_unreadable_or_foreign_settings_file_reads_as_the_defaults(tmp_path: Path) -> None:
    store = ResourceGaugeSettingsStore(state_directory=tmp_path)
    path = tmp_path / RESOURCE_GAUGE_FILENAME
    path.write_text("{not json")
    assert store.read() == DEFAULT_RESOURCE_GAUGE_SETTINGS
    path.write_text(json.dumps({"version": 2, "visibility": "always"}))
    assert store.read() == DEFAULT_RESOURCE_GAUGE_SETTINGS
    path.write_text(json.dumps({"version": 1, "memory_warning_percent": 120}))
    assert store.read() == DEFAULT_RESOURCE_GAUGE_SETTINGS


def test_warning_levels_out_of_range_are_refused() -> None:
    with pytest.raises(ValidationError):
        ResourceGaugeSettings(disk_warning_free_gb=0.1)
    with pytest.raises(ValidationError):
        ResourceGaugeSettings(memory_warning_percent=49)
    with pytest.raises(ValidationError):
        ResourceGaugeSettings(cpu_warning_percent=100)


def test_the_disk_warns_on_space_left_and_clears_only_with_a_fifth_more_free() -> None:
    settings = ResourceGaugeSettings(disk_warning_free_gb=5.0)
    # 5.1 GB of 100 is 95% used, and no warning: the share used is not what is judged.
    raised = next_warnings(frozenset(), _readings(disk_free_gb=5.1), settings)
    assert raised == frozenset()
    raised = next_warnings(raised, _readings(disk_free_gb=5.0), settings)
    assert raised == {GaugeResource.DISK}
    raised = next_warnings(raised, _readings(disk_free_gb=5.9), settings)
    assert raised == {GaugeResource.DISK}
    raised = next_warnings(raised, _readings(disk_free_gb=6.1), settings)
    assert raised == frozenset()


def test_a_disk_on_course_to_fill_within_minutes_warns_whatever_is_left() -> None:
    settings = ResourceGaugeSettings(disk_warning_free_gb=5.0)
    assert next_warnings(frozenset(), _readings(disk_free_gb=40, seconds_to_full=3600), settings) == frozenset()
    raised = next_warnings(frozenset(), _readings(disk_free_gb=40, seconds_to_full=600), settings)
    assert raised == {GaugeResource.DISK}
    alert = describe_alert(GaugeResource.DISK, _readings(disk_free_gb=40, seconds_to_full=600), settings)
    assert (alert.headline, alert.detail) == ("Disk is filling up fast", "40 GB left, about 10 minutes at this rate")
    # The filling stopped with plenty left: the warning clears.
    assert next_warnings(raised, _readings(disk_free_gb=38), settings) == frozenset()


def test_a_resource_with_no_level_never_warns_and_one_with_no_reading_keeps_its_warning() -> None:
    settings = ResourceGaugeSettings(disk_warning_free_gb=5.0, memory_warning_percent=None)
    raised = next_warnings(frozenset(), _readings(disk_free_gb=1, memory_percent=99), settings)
    assert raised == {GaugeResource.DISK}
    assert next_warnings(raised, NO_READINGS, settings) == {GaugeResource.DISK}
    assert next_warnings(raised, _readings(disk_free_gb=1), ResourceGaugeSettings(disk_warning_free_gb=None)) == (
        frozenset()
    )


def test_memory_warns_at_its_level_and_clears_five_points_below() -> None:
    settings = ResourceGaugeSettings(disk_warning_free_gb=None, memory_warning_percent=90)
    raised = next_warnings(frozenset(), _readings(memory_percent=90), settings)
    assert raised == {GaugeResource.MEMORY}
    assert next_warnings(raised, _readings(memory_percent=86), settings) == {GaugeResource.MEMORY}
    assert next_warnings(raised, _readings(memory_percent=85), settings) == frozenset()


def test_the_cpu_warning_is_judged_on_the_recent_mean_not_the_latest_reading() -> None:
    settings = ResourceGaugeSettings(disk_warning_free_gb=None, cpu_warning_percent=80)
    spike = ResourceReadings(cpu=CpuReading(percent=100, recent_percent=40, cores=4), memory=None, disk=None)
    assert next_warnings(frozenset(), spike, settings) == frozenset()
    sustained = ResourceReadings(cpu=CpuReading(percent=60, recent_percent=85, cores=4), memory=None, disk=None)
    assert next_warnings(frozenset(), sustained, settings) == {GaugeResource.CPU}
    assert describe_alert(GaugeResource.CPU, sustained, settings).detail == "85% in use over the last two minutes"


def test_the_time_to_full_comes_from_the_fill_rate_once_a_minute_of_samples_is_in() -> None:
    gib = GIB
    # 1 GB a minute with 10 GB left: ten minutes.
    assert estimate_seconds_to_full([(0.0, 12 * gib), (120.0, 10 * gib)], 10 * gib) == pytest.approx(600.0)
    # Too short a span to trust, a disk that is not filling, and a fill more than a day out say nothing.
    assert estimate_seconds_to_full([(0.0, 12 * gib), (30.0, 10 * gib)], 10 * gib) is None
    assert estimate_seconds_to_full([(0.0, 10 * gib), (120.0, 12 * gib)], 12 * gib) is None
    assert estimate_seconds_to_full([(0.0, 500 * gib + 1024), (120.0, 500 * gib)], 500 * gib) is None
    assert estimate_seconds_to_full([(0.0, 10 * gib)], 10 * gib) is None


def test_sizes_and_spans_are_said_roughly() -> None:
    assert format_gigabytes(int(3.24 * GIB)) == "3.2 GB"
    assert format_gigabytes(40 * GIB) == "40 GB"
    assert format_duration(20) == "under a minute"
    assert format_duration(60) == "1 minute"
    assert format_duration(12.4 * 60) == "12 minutes"
    assert format_duration(3 * 3600) == "3 hours"


def test_an_alert_says_what_is_left_and_turns_critical_at_a_fifth_of_the_level() -> None:
    settings = ResourceGaugeSettings(disk_warning_free_gb=5.0, memory_warning_percent=90)
    low = describe_alert(GaugeResource.DISK, _readings(disk_free_gb=3.2), settings)
    assert (low.severity, low.headline, low.detail) == (AlertSeverity.WARNING, "Disk space is low", "3.2 GB left")
    critical = describe_alert(GaugeResource.DISK, _readings(disk_free_gb=0.9, seconds_to_full=240), settings)
    assert (critical.severity, critical.headline) == (AlertSeverity.CRITICAL, "Disk is almost full")
    assert critical.detail == "0.9 GB left, about 4 minutes at this rate"
    memory = describe_alert(GaugeResource.MEMORY, _readings(memory_percent=95), settings)
    assert (memory.headline, memory.detail) == ("Memory is nearly full", "0.4 GB left of 8.0 GB")
    title, message = describe_notification(low)
    assert title == "Disk space is low: 3.2 GB left"
    assert "System Monitor" in message


def _recent_file(sampled_at: float, usage: float, samples: list[list[float]]) -> dict[str, object]:
    return {
        "version": 1,
        "sampled_at": sampled_at,
        "interval_seconds": 5,
        "cores": 4,
        "cpu": {"usage": usage, "by_pid": {}},
        "samples": samples,
    }


def test_the_cpu_reading_averages_the_samples_of_the_last_two_minutes() -> None:
    now = 1_000_000.0
    samples = [
        [(now - 300) * 1000, 1.0, 0.5],
        [(now - 100) * 1000, 0.5, 0.5],
        [(now - 10) * 1000, 0.7, 0.5],
    ]
    reading = parse_cpu_reading(_recent_file(now - 2, 0.25, samples), now)
    assert reading is not None
    assert reading.percent == pytest.approx(25.0)
    assert reading.recent_percent == pytest.approx(60.0)
    assert reading.cores == 4


def test_a_stale_foreign_or_missing_cpu_reading_is_none(tmp_path: Path) -> None:
    now = 1_000_000.0
    assert parse_cpu_reading(_recent_file(now - 31, 0.5, []), now) is None
    assert parse_cpu_reading({**_recent_file(now, 0.5, []), "version": 2}, now) is None
    assert parse_cpu_reading({**_recent_file(now, 0.5, []), "cpu": {}}, now) is None
    assert read_cpu(tmp_path / "absent.json") is None
    (tmp_path / "recent.json").write_text(json.dumps(_recent_file(now, 0.5, [])))
    reading = read_cpu(tmp_path / "recent.json", clock=lambda: now)
    assert reading is not None
    assert reading.recent_percent == pytest.approx(50.0)


def test_the_monitor_notifies_once_per_warning_and_again_only_after_it_cleared(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    for free_gb in (50.0, 4.0, 3.9, 20.0, 4.5):
        harness.read(disk_free_gb=free_gb)
    assert harness.titles() == ["Disk space is low: 4.0 GB left", "Disk space is low: 4.5 GB left"]
    assert [alert.resource for alert in harness.monitor.view().alerts] == [GaugeResource.DISK]
    assert harness.monitor.view().readings.disk == _disk(4.5)


def test_a_restart_of_the_shell_does_not_notify_a_standing_warning_again(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.read(disk_free_gb=4.0)
    assert len(harness.titles()) == 1
    restarted = harness.build()
    harness.read(restarted, disk_free_gb=3.5)
    assert len(harness.titles()) == 1
    # Cleared and raised again after the restart: that is a new warning.
    harness.read(restarted, disk_free_gb=30.0)
    harness.read(restarted, disk_free_gb=2.0)
    assert len(harness.titles()) == 2


def test_a_notification_that_could_not_be_delivered_is_retried_after_a_minute(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.notifier.is_reachable = False
    harness.read(disk_free_gb=4.0)
    harness.notifier.is_reachable = True
    harness.read(disk_free_gb=4.0)
    assert harness.titles() == []
    harness.read(disk_free_gb=4.0, advance_seconds=NOTIFY_RETRY_SECONDS)
    assert harness.titles() == ["Disk space is low: 4.0 GB left"]


def test_saving_settings_judges_the_last_reading_at_once_without_sending_from_the_request(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.read(disk_free_gb=8.0, memory_percent=60)
    assert harness.monitor.view().alerts == ()
    view = harness.monitor.update_settings(
        ResourceGaugeSettings(visibility=GaugeVisibility.ALWAYS, disk_warning_free_gb=10.0, memory_warning_percent=55)
    )
    assert [alert.resource for alert in view.alerts] == [GaugeResource.DISK, GaugeResource.MEMORY]
    assert harness.pushed[-1] == view
    assert harness.titles() == []
    harness.read()
    assert [view for _, view in harness.notifier.sent] == [GaugeResource.DISK, GaugeResource.MEMORY]
    assert ResourceGaugeSettingsStore(state_directory=tmp_path).read().disk_warning_free_gb == 10.0


def test_the_disks_projection_comes_from_the_monitors_own_samples(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.read(disk_free_gb=40.0)
    assert harness.monitor.view().readings.disk == _disk(40.0)
    harness.read(disk_free_gb=38.0, advance_seconds=120.0)
    disk = harness.monitor.view().readings.disk
    assert disk is not None and disk.seconds_to_full == pytest.approx(38 * 60.0, rel=0.01)


def test_programs_the_memory_guard_stops_are_reported_but_not_the_ledgers_history(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.stops = [StoppedProgram(at="2026-11-12T10:00:00.000000Z", what="a background program")]
    harness.read(disk_free_gb=50.0)
    assert harness.titles() == []
    harness.stops = [
        *harness.stops,
        StoppedProgram(at="2026-11-12T10:05:00.000000Z", what="the Report chat's agent"),
    ]
    harness.read()
    assert harness.notifier.sent == [("Stopped the Report chat's agent to free memory", GaugeResource.MEMORY)]
    harness.read()
    harness.read(harness.build())
    assert len(harness.titles()) == 1
    title, _ = describe_stops_notification(harness.stops)
    assert title == "Stopped 2 programs to free memory"


def test_the_view_lists_the_last_days_stops_newest_first(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    # 2027-01-15T08:00:00Z
    harness.now = 1_800_000_000.0
    harness.stops = [
        StoppedProgram(at="2027-01-13T09:00:00.000000Z", what="a background program"),
        StoppedProgram(at="2027-01-15T06:00:00.000000Z", what="a background agent"),
        StoppedProgram(at="2027-01-15T07:00:00.000000Z", what="a chat's agent"),
    ]
    harness.read(disk_free_gb=50.0)
    assert [stop.what for stop in harness.monitor.view().recent_stops] == ["a chat's agent", "a background agent"]


def test_the_shed_ledger_is_read_in_plain_words_and_only_again_when_it_changes(tmp_path: Path) -> None:
    ledger = tmp_path / "shed.jsonl"
    reader = StopLedgerReader(ledger_path=ledger)
    assert reader.read() == ()
    records: Sequence[dict[str, Any]] = [
        {"timestamp": "2026-11-12T10:00:00.000000Z", "type": "process_shed", "comm": "node", "agent_name": None},
        {"timestamp": "2026-11-12T10:01:00.000000Z", "type": "notice_delivered", "agent_name": "Report-Agent"},
        {"timestamp": "2026-11-12T10:02:00.000000Z", "type": "process_shed", "agent_name": "Report-Agent"},
        {"timestamp": "2026-11-12T10:03:00.000000Z", "type": "process_shed", "comm": "claude", "agent_name": None},
        {"timestamp": "2026-11-12T10:04:00.000000Z", "type": "process_shed", "comm": "x", "is_worker": True},
    ]
    ledger.write_text("".join(json.dumps(record) + "\n" for record in records) + "{half a line")
    assert [stop.what for stop in reader.read()] == [
        "a background program",
        "the Report chat's agent",
        "a chat's agent",
        "a background agent",
    ]
    assert reader.read() is reader.read()


def test_the_windows_are_told_of_what_they_draw_and_once_a_minute_regardless(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.read(disk_free_gb=50.0, memory_percent=40)
    assert len(harness.pushed) == 1
    # Hidden until a warning: a moving reading is nobody's to draw.
    harness.read(disk_free_gb=45.0, memory_percent=70)
    assert len(harness.pushed) == 1
    harness.read(disk_free_gb=4.0, memory_percent=70)
    assert len(harness.pushed) == 2 and harness.pushed[-1].alerts[0].headline == "Disk space is low"
    harness.read(disk_free_gb=4.0, memory_percent=70, advance_seconds=PUSH_HEARTBEAT_SECONDS)
    assert len(harness.pushed) == 3
    # Always shown: a bar that moved two points is redrawn, one that moved less is not.
    harness.monitor.update_settings(ResourceGaugeSettings(visibility=GaugeVisibility.ALWAYS))
    assert len(harness.pushed) == 4
    harness.read(disk_free_gb=4.0, memory_percent=71)
    assert len(harness.pushed) == 4
    harness.read(disk_free_gb=4.0, memory_percent=73)
    assert len(harness.pushed) == 5


def test_the_agents_notice_stands_while_a_warning_does(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    notice_path = tmp_path / AGENT_NOTICE_FILENAME
    harness.read(disk_free_gb=50.0)
    assert not notice_path.exists()
    harness.read(disk_free_gb=3.2)
    notice = notice_path.read_text()
    assert notice.startswith("Workspace resource notice")
    assert "- Disk space is low: 3.2 GB left. Do not write large files" in notice
    harness.read(disk_free_gb=50.0)
    assert not notice_path.exists()
    assert describe_agent_notice(()) == ""


def test_a_monitor_with_no_notifier_raises_warnings_and_tells_nobody(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, has_notifier=False)
    harness.read(disk_free_gb=2.0)
    assert [alert.severity for alert in harness.monitor.view().alerts] == [AlertSeverity.WARNING]
    assert harness.titles() == []
    assert DeliveryStore(state_directory=tmp_path).read() == DeliveryRecord(notified=(), last_stop_timestamp="")


_NOTIFICATIONS_PATH = "/minds-api-proxy/api/v1/agents/agent-services/notifications"


def _gateway_environ(base_url: str) -> dict[str, str]:
    return {
        "LATCHKEY_GATEWAY": base_url + "/",
        "LATCHKEY_GATEWAY_PASSWORD": "secret",
        "MNGR_AGENT_ID": "agent-services",
    }


def test_a_notification_is_posted_to_the_studio_feed_and_opens_the_resources_view() -> None:
    received: list[dict[str, Any]] = []
    with serve_app(message_handling_app(received, _NOTIFICATIONS_PATH, 200)) as served:
        notifier = GatewayResourceNotifier(environ=_gateway_environ(served.http_url))
        assert notifier.notify("Disk space is low: 3.2 GB left", "Open System Monitor.", GaugeResource.DISK)
    assert received == [
        {
            "title": "Disk space is low: 3.2 GB left",
            "message": "Open System Monitor.",
            "destination": {"type": "minds:show-system-monitor", "view": "disk"},
        }
    ]


def test_a_refused_unreachable_or_unconfigured_notification_is_not_taken() -> None:
    received: list[dict[str, Any]] = []
    with serve_app(message_handling_app(received, _NOTIFICATIONS_PATH, 403)) as served:
        assert not GatewayResourceNotifier(environ=_gateway_environ(served.http_url)).notify(
            "Disk space is low", "x", GaugeResource.DISK
        )
    assert len(received) == 1
    unreachable = GatewayResourceNotifier(environ=_gateway_environ("http://127.0.0.1:9"))
    assert not unreachable.notify("Disk space is low", "x", GaugeResource.DISK)
    assert not GatewayResourceNotifier(environ={}).notify("Disk space is low", "x", GaugeResource.DISK)
