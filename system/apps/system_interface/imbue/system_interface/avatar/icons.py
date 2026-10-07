"""The avatar as a home-screen icon: a design's still pose centred on an opaque square tile, rasterized to PNG.

A phone's home screen takes a PNG (iOS fills any transparency with black), and the design's motion is CSS a
rasterizer does not run, so the icon is the preview pose ``render_design_svg`` holds still.
"""

from typing import Final
from xml.etree.ElementTree import Element
from xml.etree.ElementTree import tostring

import resvg_py

from imbue.system_interface.avatar.designs import AvatarMood
from imbue.system_interface.avatar.designs import REQUIRED_VIEW_BOX
from imbue.system_interface.avatar.designs import SVG_NAMESPACE
from imbue.system_interface.avatar.designs import render_design_element
from imbue.system_interface.documents import SHELL_BACKGROUND_COLOR
from imbue.system_interface.shell.errors import InvalidShellValueError

DEFAULT_ICON_SIZE: Final[int] = 180
MIN_ICON_SIZE: Final[int] = 16
MAX_ICON_SIZE: Final[int] = 1024
# The design's margin inside the tile, in the design's own 100-unit grid: room for the corners a home screen rounds.
_ICON_INSET: Final[int] = 10


def _icon_tile(svg: str, design_id: str) -> Element:
    """The tile's drawing: the design's still pose inset on the square."""
    design = render_design_element(svg, AvatarMood.IDLE, True, design_id)
    extent = 100 - 2 * _ICON_INSET
    design.set("x", str(_ICON_INSET))
    design.set("y", str(_ICON_INSET))
    design.set("width", str(extent))
    design.set("height", str(extent))
    tile = Element(f"{{{SVG_NAMESPACE}}}svg", {"viewBox": REQUIRED_VIEW_BOX})
    tile.append(design)
    return tile


def render_design_icon_png(svg: str, design_id: str, size: int) -> bytes:
    """The design's icon as a ``size`` pixel square PNG on the shell's page colour; raises InvalidShellValueError for
    a size outside the bounds."""
    if not MIN_ICON_SIZE <= size <= MAX_ICON_SIZE:
        raise InvalidShellValueError(f"an icon size is {MIN_ICON_SIZE} to {MAX_ICON_SIZE} pixels, not {size}")
    return resvg_py.svg_to_bytes(
        svg_string=tostring(_icon_tile(svg, design_id), encoding="unicode"),
        width=size,
        height=size,
        background=SHELL_BACKGROUND_COLOR,
    )
