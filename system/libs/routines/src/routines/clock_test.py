from datetime import datetime
from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from routines.clock import resolve_local_zone


def test_the_zone_comes_from_tz_first(tmp_path: Path) -> None:
    zone = resolve_local_zone({"TZ": "Europe/Prague"}, tmp_path / "timezone", tmp_path / "localtime", tmp_path)
    assert zone == ZoneInfo("Europe/Prague")


def test_the_zone_comes_from_the_timezone_file_then_the_localtime_link(tmp_path: Path) -> None:
    zoneinfo_dir = tmp_path / "zoneinfo"
    (zoneinfo_dir / "America").mkdir(parents=True)
    (zoneinfo_dir / "America" / "Los_Angeles").write_bytes(b"")
    localtime = tmp_path / "localtime"
    localtime.symlink_to(zoneinfo_dir / "America" / "Los_Angeles")
    timezone_file = tmp_path / "timezone"

    assert resolve_local_zone({}, timezone_file, localtime, zoneinfo_dir) == ZoneInfo("America/Los_Angeles")
    timezone_file.write_text("Asia/Tokyo\n")
    assert resolve_local_zone({}, timezone_file, localtime, zoneinfo_dir) == ZoneInfo("Asia/Tokyo")


def test_a_zone_with_daylight_saving_moves_a_wall_clock_time_across_the_change(tmp_path: Path) -> None:
    zone = resolve_local_zone({"TZ": "America/Los_Angeles"}, tmp_path / "timezone", tmp_path / "localtime", tmp_path)
    before = datetime(2026, 10, 31, 7, 30, tzinfo=zone)
    after = datetime(2026, 11, 2, 7, 30, tzinfo=zone)
    assert before.utcoffset() != after.utcoffset()


def test_an_unknown_name_everywhere_falls_back_to_the_current_offset(tmp_path: Path) -> None:
    (tmp_path / "timezone").write_text("Nowhere/Nothing\n")
    zone = resolve_local_zone({"TZ": "Not/AZone"}, tmp_path / "timezone", tmp_path / "localtime", tmp_path)
    now = datetime.now(timezone.utc)
    assert not isinstance(zone, ZoneInfo)
    assert zone.utcoffset(now) == now.astimezone().utcoffset()
