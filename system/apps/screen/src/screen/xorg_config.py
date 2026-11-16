"""The ``xorg.conf`` the Screen's X server runs with (docs/system/specs/screen/spec.md section 7.1): Xorg's dummy
driver on one virtual screen sized to the 1080p cap, seeded with three CVT modes, and neko's ``xf86-input-neko``
touchscreen, the device a phone's touches reach the Screen through (section 14)."""

import math
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from screen.primitives import MAX_HEIGHT
from screen.primitives import MAX_WIDTH
from screen.primitives import RUNTIME_DIR
from screen.primitives import SEEDED_MODES

# The dummy driver's framebuffer, in kilobytes: the 1080p cap at 32 bpp is 8100 KB, so this leaves room for the
# RANDR modes added later and anything up to 4K.
VIDEO_RAM_KB: Final[int] = 65536

# The refresh every mode is computed for; the mode name carries it (``1920x1080_60``).
MODE_REFRESH_HZ: Final[int] = 60

# neko's Xorg input driver (``neko_drv.so``, the ``neko`` driver): a direct-touch device whose events arrive over a
# Unix socket the driver binds when Xorg initializes it, and that neko connects to at its start. Mouse and keyboard
# input stays on XTEST. The identifier is what the Xorg log names the device by.
INPUT_DRIVER: Final[str] = "neko"
INPUT_DEVICE_IDENTIFIER: Final[str] = "screen_touchscreen"
INPUT_SOCKET_PATH: Final[str] = f"{RUNTIME_DIR}/xf86-input-neko.sock"

# The CVT 1.2 constants (VESA Coordinated Video Timings), as ``cvt`` applies them: the blanking formula's
# parameters, the fixed back porch, and the sync width table by aspect ratio.
_CVT_CLOCK_STEP_MHZ: Final[float] = 0.25
_CVT_MIN_VSYNC_BP_US: Final[float] = 550.0
_CVT_MIN_V_PORCH: Final[int] = 3
_CVT_H_SYNC_PERCENT: Final[float] = 8.0
_CVT_CELL_GRAN: Final[int] = 8
_CVT_C_PRIME: Final[float] = 30.0
_CVT_M_PRIME: Final[float] = 300.0
_CVT_MIN_V_BPORCH: Final[int] = 6
_CVT_INTERLACE: Final[float] = 0.0


class Modeline(FrozenModel):
    """One X modeline as ``cvt`` prints it."""

    name: str = Field(description="The mode's name, ``<width>x<height>_<refresh>``")
    pixel_clock_mhz: float = Field(description="The pixel clock")
    width: int = Field(description="Horizontal active pixels")
    h_sync_start: int = Field(description="Horizontal sync start")
    h_sync_end: int = Field(description="Horizontal sync end")
    h_total: int = Field(description="Horizontal total")
    height: int = Field(description="Vertical active lines")
    v_sync_start: int = Field(description="Vertical sync start")
    v_sync_end: int = Field(description="Vertical sync end")
    v_total: int = Field(description="Vertical total")

    def render(self) -> str:
        return (
            f'Modeline "{self.name}" {self.pixel_clock_mhz:.2f} '
            f"{self.width} {self.h_sync_start} {self.h_sync_end} {self.h_total} "
            f"{self.height} {self.v_sync_start} {self.v_sync_end} {self.v_total} -HSync +VSync"
        )


@pure
def mode_name(width: int, height: int) -> str:
    return f"{width}x{height}_{MODE_REFRESH_HZ}"


@pure
def _cvt_vertical_sync_width(width: int, height: int) -> int:
    """CVT's sync width by aspect ratio (4:3, 16:9, 16:10, 5:4, 15:9), 10 for anything else."""
    aspect_widths = ((4, 3, 4), (16, 9, 5), (16, 10, 6), (5, 4, 7), (15, 9, 7))
    for aspect_width, aspect_height, sync_width in aspect_widths:
        if width == (height * aspect_width // aspect_height) // _CVT_CELL_GRAN * _CVT_CELL_GRAN:
            return sync_width
    return 10


@pure
def cvt_modeline(width: int, height: int) -> Modeline:
    """The CVT 1.2 modeline for ``width`` by ``height`` at 60 Hz, as ``cvt <w> <h> 60`` computes it."""
    h_pixels_rounded = (width // _CVT_CELL_GRAN) * _CVT_CELL_GRAN
    v_sync_width = _cvt_vertical_sync_width(width, height)
    h_period_estimate_us = (1_000_000.0 / MODE_REFRESH_HZ - _CVT_MIN_VSYNC_BP_US) / (
        height + _CVT_MIN_V_PORCH + _CVT_INTERLACE
    )
    v_sync_and_back_porch = int(_CVT_MIN_VSYNC_BP_US / h_period_estimate_us) + 1
    v_back_porch = max(v_sync_and_back_porch - v_sync_width, _CVT_MIN_V_BPORCH)
    v_sync_and_back_porch = v_back_porch + v_sync_width
    v_total = height + v_sync_and_back_porch + _CVT_MIN_V_PORCH
    ideal_duty_cycle = _CVT_C_PRIME - (_CVT_M_PRIME * h_period_estimate_us / 1000.0)
    if ideal_duty_cycle < 20.0:
        h_blank = (h_pixels_rounded * 20 // (100 - 20)) // (2 * _CVT_CELL_GRAN) * (2 * _CVT_CELL_GRAN)
    else:
        h_blank = (
            int(h_pixels_rounded * ideal_duty_cycle / (100.0 - ideal_duty_cycle))
            // (2 * _CVT_CELL_GRAN)
            * (2 * _CVT_CELL_GRAN)
        )
    h_total = h_pixels_rounded + h_blank
    h_sync_width = (int(h_total * _CVT_H_SYNC_PERCENT / 100.0) // _CVT_CELL_GRAN) * _CVT_CELL_GRAN
    pixel_clock_mhz = math.floor((h_total / h_period_estimate_us) / _CVT_CLOCK_STEP_MHZ) * _CVT_CLOCK_STEP_MHZ
    h_sync_end = h_pixels_rounded + h_blank // 2
    return Modeline(
        name=mode_name(width, height),
        pixel_clock_mhz=pixel_clock_mhz,
        width=h_pixels_rounded,
        h_sync_start=h_sync_end - h_sync_width,
        h_sync_end=h_sync_end,
        h_total=h_total,
        height=height,
        v_sync_start=height + _CVT_MIN_V_PORCH,
        v_sync_end=height + _CVT_MIN_V_PORCH + v_sync_width,
        v_total=v_total,
    )


@pure
def render_xorg_conf() -> str:
    """The dummy-driver configuration: one screen at the cap's virtual size, the seeded modes, neko's touchscreen as
    the one configured input device (mouse and keyboard arrive over XTEST), no VT, no PCI probing."""
    modelines = [cvt_modeline(width, height) for width, height in SEEDED_MODES]
    modes_list = " ".join(f'"{modeline.name}"' for modeline in modelines)
    modeline_lines = "\n".join(f"  {modeline.render()}" for modeline in modelines)
    return f"""Section "ServerFlags"
  Option "DontVTSwitch" "true"
  Option "AllowMouseOpenFail" "true"
  Option "PciForceNone" "true"
  Option "AutoEnableDevices" "false"
  Option "AutoAddDevices" "false"
EndSection

Section "InputDevice"
  Identifier "{INPUT_DEVICE_IDENTIFIER}"
  Option "SendCoreEvents" "On"
  Option "SocketName" "{INPUT_SOCKET_PATH}"
  Driver "{INPUT_DRIVER}"
EndSection

Section "Device"
  Identifier "screen_device"
  Driver "dummy"
  Option "ConstantDPI" "true"
  VideoRam {VIDEO_RAM_KB}
EndSection

Section "Monitor"
  Identifier "screen_monitor"
  HorizSync 5.0 - 1000.0
  VertRefresh 5.0 - 200.0
{modeline_lines}
EndSection

Section "Screen"
  Identifier "screen"
  Device "screen_device"
  Monitor "screen_monitor"
  DefaultDepth 24
  SubSection "Display"
    Viewport 0 0
    Depth 24
    Modes {modes_list}
    Virtual {MAX_WIDTH} {MAX_HEIGHT}
  EndSubSection
EndSection

Section "ServerLayout"
  Identifier "screen_layout"
  Screen "screen"
  InputDevice "{INPUT_DEVICE_IDENTIFIER}" "CorePointer"
EndSection
"""
