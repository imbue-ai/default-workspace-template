import json
from pathlib import Path

import pytest
from inline_snapshot import snapshot

from screen.data_types import AddedMode
from screen.data_types import DisplayModes
from screen.data_types import ScreenSize
from screen.errors import DisplayModeError
from screen.sizing import ModesLedger
from screen.sizing import decide_mode_addition
from screen.sizing import modeline_arguments
from screen.sizing import parse_xrandr_query
from screen.sizing import quantize_screen_size
from screen.xorg_config import cvt_modeline
from screen.xorg_config import mode_name

# ``xrandr --query`` on the dummy driver with the seeded xorg.conf, plus a disconnected output whose modes must not
# count and a mode added earlier this life.
_XRANDR_OUTPUT = """\
Screen 0: minimum 8 x 8, current 1920 x 1080, maximum 1920 x 1080
DUMMY1 disconnected (normal left inverted right x axis y axis)
   800x600_60      60.00
DUMMY0 connected primary 1920x1080+0+0 (normal left inverted right x axis y axis) 0mm x 0mm
   1920x1080_60   60.00*+
   1600x900_60    60.00
   1280x720_60    60.00
   1424x784_60    59.94
"""


@pytest.mark.parametrize(
    ("width", "height", "expected"),
    [
        (1437, 791, (1424, 784)),
        (1920, 1080, (1920, 1080)),
        (2560, 1440, (1920, 1080)),
        (300, 200, (640, 480)),
        (655, 495, (640, 480)),
        (1, 1, (640, 480)),
        (1600, 900, (1600, 896)),
        (1900, 1079, (1888, 1072)),
    ],
)
def test_sizes_round_down_to_sixteen_and_clamp_to_the_floor_and_the_cap(
    width: int, height: int, expected: tuple[int, int]
) -> None:
    size = quantize_screen_size(width, height)
    assert (size.width, size.height) == expected
    assert size.rate == 60


def test_mode_names_carry_the_size_and_the_refresh() -> None:
    assert mode_name(1424, 784) == snapshot("1424x784_60")
    assert cvt_modeline(1424, 784).name == mode_name(1424, 784)


def test_xrandr_query_parses_the_connected_outputs_modes_only() -> None:
    assert parse_xrandr_query(_XRANDR_OUTPUT) == snapshot(
        DisplayModes(output="DUMMY0", mode_names=("1920x1080_60", "1600x900_60", "1280x720_60", "1424x784_60"))
    )


def test_xrandr_query_with_no_connected_output_parses_to_none() -> None:
    assert parse_xrandr_query("Screen 0: minimum 8 x 8, current 1920 x 1080, maximum 1920 x 1080\n") == DisplayModes(
        output=None, mode_names=()
    )


def test_an_advertised_mode_needs_no_addition() -> None:
    modes = parse_xrandr_query(_XRANDR_OUTPUT)
    assert decide_mode_addition(modes, ScreenSize(width=1424, height=784, rate=60)) is None
    assert decide_mode_addition(modes, ScreenSize(width=1280, height=720, rate=60)) is None


def test_a_new_size_is_added_to_the_connected_output_with_its_cvt_modeline() -> None:
    addition = decide_mode_addition(parse_xrandr_query(_XRANDR_OUTPUT), ScreenSize(width=1024, height=768, rate=60))
    assert addition is not None
    assert addition.output == "DUMMY0"
    assert modeline_arguments(addition.modeline) == snapshot(
        ["1024x768_60", "63.50", "1024", "1072", "1176", "1328", "768", "771", "775", "798", "-HSync", "+VSync"]
    )


def test_no_connected_output_is_an_error() -> None:
    with pytest.raises(DisplayModeError, match="no connected output"):
        decide_mode_addition(DisplayModes(output=None, mode_names=()), ScreenSize(width=1024, height=768, rate=60))


def test_the_modes_ledger_appends_once_per_name_and_clears(tmp_path: Path) -> None:
    ledger = ModesLedger(path=tmp_path / "state" / "modes.json")
    assert ledger.read().modes == ()
    ledger.append(AddedMode(name="1024x768_60", width=1024, height=768))
    ledger.append(AddedMode(name="1024x768_60", width=1024, height=768))
    ledger.append(AddedMode(name="1424x784_60", width=1424, height=784))
    assert [mode.name for mode in ledger.read().modes] == ["1024x768_60", "1424x784_60"]
    assert json.loads(ledger.path.read_text())["modes"][1] == {"name": "1424x784_60", "width": 1424, "height": 784}
    ledger.clear()
    assert not ledger.path.exists()
    assert ledger.read().modes == ()


def test_an_unreadable_ledger_reads_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "modes.json"
    path.write_text("{not json")
    assert ModesLedger(path=path).read().modes == ()
