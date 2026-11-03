"""Sizing the display (docs/system/specs/screen/spec.md section 7.2): a requested size quantized and clamped, the
RANDR mode it needs added with ``xrandr`` when the server does not advertise it yet, and the record of the modes
added this stack life."""

import json
import re
import subprocess
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from screen.data_types import AddedMode
from screen.data_types import DisplayModes
from screen.data_types import ModeAddition
from screen.data_types import ModesRecord
from screen.data_types import ScreenSize
from screen.errors import DisplayModeError
from screen.interfaces import DisplayModesInterface
from screen.primitives import DISPLAY
from screen.primitives import MAX_HEIGHT
from screen.primitives import MAX_WIDTH
from screen.primitives import MIN_HEIGHT
from screen.primitives import MIN_WIDTH
from screen.primitives import SIZE_QUANTUM_PX
from screen.xorg_config import MODE_REFRESH_HZ
from screen.xorg_config import Modeline
from screen.xorg_config import cvt_modeline
from screen.xorg_config import mode_name

MODES_RECORD_FILENAME: Final[str] = "modes.json"
_XRANDR_TIMEOUT_SECONDS: Final[float] = 10.0

# ``xrandr --query`` prints each output as ``<name> connected ...`` or ``<name> disconnected ...`` and the modes it
# advertises indented beneath it, the mode name first.
_OUTPUT_LINE: Final[re.Pattern[str]] = re.compile(r"^(?P<name>\S+) (?P<state>connected|disconnected)\b")
_MODE_LINE: Final[re.Pattern[str]] = re.compile(r"^\s+(?P<name>\S+)\s")


@pure
def _quantize_dimension(value: int, floor: int, cap: int) -> int:
    """Rounded down to the quantum and clamped; a value at or past the cap is the cap itself, since the cap (1080
    lines) is not a multiple of the quantum and a full-size viewer must get the full-size mode."""
    if value >= cap:
        return cap
    return max((value // SIZE_QUANTUM_PX) * SIZE_QUANTUM_PX, floor)


@pure
def quantize_screen_size(width: int, height: int) -> ScreenSize:
    """The size a viewer reported, rounded down to the quantum and clamped to the floor and the cap (spec D7)."""
    return ScreenSize(
        width=_quantize_dimension(width, MIN_WIDTH, MAX_WIDTH),
        height=_quantize_dimension(height, MIN_HEIGHT, MAX_HEIGHT),
        rate=MODE_REFRESH_HZ,
    )


@pure
def parse_xrandr_query(output: str) -> DisplayModes:
    """The connected output and its modes from ``xrandr --query``; a disconnected output's modes are skipped."""
    connected: str | None = None
    is_under_connected = False
    names: list[str] = []
    for line in output.splitlines():
        output_match = _OUTPUT_LINE.match(line)
        if output_match is not None:
            is_under_connected = output_match.group("state") == "connected" and connected is None
            if is_under_connected:
                connected = output_match.group("name")
            continue
        mode_match = _MODE_LINE.match(line)
        if mode_match is not None and is_under_connected:
            names.append(mode_match.group("name"))
    return DisplayModes(output=connected, mode_names=tuple(names))


@pure
def decide_mode_addition(modes: DisplayModes, size: ScreenSize) -> ModeAddition | None:
    """The mode to add for ``size``, or None when the output already advertises it."""
    if modes.output is None:
        raise DisplayModeError("xrandr reports no connected output to add a mode to")
    if mode_name(size.width, size.height) in modes.mode_names:
        return None
    return ModeAddition(output=modes.output, modeline=cvt_modeline(size.width, size.height))


@pure
def modeline_arguments(modeline: Modeline) -> list[str]:
    """The ``--newmode`` arguments for a modeline, as ``cvt`` prints them."""
    return [
        modeline.name,
        f"{modeline.pixel_clock_mhz:.2f}",
        str(modeline.width),
        str(modeline.h_sync_start),
        str(modeline.h_sync_end),
        str(modeline.h_total),
        str(modeline.height),
        str(modeline.v_sync_start),
        str(modeline.v_sync_end),
        str(modeline.v_total),
        "-HSync",
        "+VSync",
    ]


class XrandrDisplayModes(DisplayModesInterface):
    """Runs ``xrandr`` against the Screen's display."""

    display: str = Field(default=DISPLAY, frozen=True, description="The X display")

    def _run(self, arguments: list[str]) -> str:
        command = ["xrandr", *arguments]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=_XRANDR_TIMEOUT_SECONDS,
                check=False,
                env={"DISPLAY": self.display, "PATH": "/usr/local/bin:/usr/bin:/bin"},
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise DisplayModeError(f"xrandr failed: {e}") from e
        if completed.returncode != 0:
            raise DisplayModeError(f"xrandr {' '.join(arguments)} failed: {completed.stderr.strip()}")
        return completed.stdout

    def query(self) -> DisplayModes:
        return parse_xrandr_query(self._run(["--query"]))

    def add_mode(self, addition: ModeAddition) -> None:
        self._run(["--newmode", *modeline_arguments(addition.modeline)])
        self._run(["--addmode", addition.output, addition.modeline.name])


class ModesLedger(FrozenModel):
    """``modes.json``: the modes added this stack life (spec section 10.3), so a restarted service knows them."""

    path: Path = Field(description="data/.state/screen/modes.json")

    def read(self) -> ModesRecord:
        if not self.path.is_file():
            return ModesRecord()
        try:
            return ModesRecord.model_validate_json(self.path.read_text())
        except (OSError, ValidationError) as e:
            logger.warning("Ignored an unreadable modes record at {}: {}", self.path, e)
            return ModesRecord()

    def append(self, mode: AddedMode) -> ModesRecord:
        record = self.read()
        if any(existing.name == mode.name for existing in record.modes):
            return record
        updated = ModesRecord(modes=(*record.modes, mode))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(updated.model_dump(mode="json"), indent=2))
        return updated

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
