from pathlib import Path

import pytest

from workspace_profile.errors import InvalidTimezoneError
from workspace_profile.timezone import (
    USER_TIMEZONE_RELATIVE_PATH,
    read_user_timezone,
    write_user_timezone,
)


def test_a_workspace_with_no_timezone_file_records_none(tmp_path: Path) -> None:
    assert read_user_timezone(tmp_path) == ""


def test_a_zone_is_written_when_none_is_recorded(tmp_path: Path) -> None:
    assert write_user_timezone(tmp_path, "Europe/Berlin", is_replacing=False) is True

    assert (tmp_path / USER_TIMEZONE_RELATIVE_PATH).read_text() == "Europe/Berlin\n"
    assert read_user_timezone(tmp_path) == "Europe/Berlin"


def test_a_recorded_zone_is_kept_unless_the_write_replaces(tmp_path: Path) -> None:
    write_user_timezone(tmp_path, "Europe/Berlin", is_replacing=False)

    assert write_user_timezone(tmp_path, "America/New_York", is_replacing=False) is False
    assert read_user_timezone(tmp_path) == "Europe/Berlin"

    assert write_user_timezone(tmp_path, "America/New_York", is_replacing=True) is True
    assert read_user_timezone(tmp_path) == "America/New_York"


@pytest.mark.parametrize("zone", ["", "Not/AZone", "../../etc/passwd", "Europe/Berlin; rm -rf /"])
def test_a_name_that_is_not_a_zone_is_refused_and_nothing_is_written(tmp_path: Path, zone: str) -> None:
    with pytest.raises(InvalidTimezoneError):
        write_user_timezone(tmp_path, zone, is_replacing=True)

    assert not (tmp_path / USER_TIMEZONE_RELATIVE_PATH).exists()
