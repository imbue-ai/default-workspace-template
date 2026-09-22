"""Test helpers for the avatar package: a minimal design the validator accepts, and a registration of it."""

from typing import Final

from imbue.system_interface.avatar.catalog import DesignRegistration
from imbue.system_interface.avatar.primitives import DesignId

# A body and closed-eye group on the 100 by 100 grid: the least a design that renders an expression needs.
MINIMAL_DESIGN_SVG: Final[str] = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
    '<g class="jelly-body"><circle cx="50" cy="60" r="30" fill="#8cd"/></g>'
    '<g class="jelly-eyes"><ellipse cx="42" cy="55" rx="2" ry="3"/><ellipse cx="58" cy="55" rx="2" ry="3"/></g>'
    "</svg>"
)


def design_registration(design_id: str, label: str = "Mine", svg: str = MINIMAL_DESIGN_SVG) -> DesignRegistration:
    """A registration of ``svg`` under ``design_id``, drawn at ``/tmp/<design_id>.svg``."""
    return DesignRegistration(id=DesignId(design_id), label=label, svg=svg, source_path=f"/tmp/{design_id}.svg")
