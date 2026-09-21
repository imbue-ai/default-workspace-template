"""Avatar designs (pinned-taskbar-entries plan section 3.5): the bundled drawings, the validator a registered
drawing must pass, and the renderer that turns a design and a mood into one isolated SVG image.

A design is passive drawing markup on a 100 by 100 grid: an allowlist of elements and attributes, local gradient
paints only, no scripts, links, images, foreign content, or stylesheets beyond the shared one the shell ships. The
renderer never edits a stored original; it sets the mood as a data attribute on a copy, injects the shared
stylesheet, and for a bundled design draws its rest or busy eyes and its motion.
"""

import re
from enum import auto
from pathlib import Path
from typing import Final
from xml.etree.ElementTree import Element
from xml.etree.ElementTree import ParseError
from xml.etree.ElementTree import SubElement
from xml.etree.ElementTree import tostring

from defusedxml.ElementTree import fromstring
from defusedxml.common import DefusedXmlException
from pydantic import Field

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.avatar.primitives import DesignId
from imbue.system_interface.shell.errors import InvalidShellValueError

ASSET_DIRECTORY: Final[Path] = Path(__file__).parent / "assets"
MAX_SVG_BYTES: Final[int] = 256 * 1024
SVG_NAMESPACE: Final[str] = "http://www.w3.org/2000/svg"
REQUIRED_VIEW_BOX: Final[str] = "0 0 100 100"
# The stylesheet every design animates through; a registered drawing may embed exactly it and nothing else.
ANIMATION_CSS: Final[str] = (ASSET_DIRECTORY / "animation.css").read_text(encoding="utf-8")
_BUILTIN_MOTION_CSS: Final[str] = (ASSET_DIRECTORY / "builtin-motion.css").read_text(encoding="utf-8")
_SEAL_MOTION_CSS: Final[str] = (ASSET_DIRECTORY / "seal-motion.css").read_text(encoding="utf-8")
_PREVIEW_CSS: Final[str] = "*{animation:none!important}"
_STYLE_TAG: Final[str] = f"{{{SVG_NAMESPACE}}}style"
_BODY_CLASS: Final[str] = "jelly-body"
_EYES_CLASS: Final[str] = "jelly-eyes"
_FLIPPERS_CLASS: Final[str] = "jelly-flippers"


class AvatarMood(LowerCaseStrEnum):
    """What the avatar's image expresses: at rest, or with an agent working (a wire value: ``data-mood``)."""

    IDLE = auto()
    WORKING = auto()


class BundledDesign(FrozenModel):
    """One of the drawings the shell ships."""

    id: DesignId = Field(description="The catalog id")
    label: str = Field(description="What the chooser calls it")
    filename: str = Field(description="The drawing's file under the asset directory")
    # The eye centres of the approved drawing: the left eye's x, the right eye's x, and their y.
    eyes_left_x: int = Field(description="The left eye's centre, x")
    eyes_right_x: int = Field(description="The right eye's centre, x")
    eyes_y: int = Field(description="Both eyes' centre, y")


DEFAULT_DESIGN_ID: Final[DesignId] = DesignId("gummy-seal")

BUNDLED_DESIGNS: Final[tuple[BundledDesign, ...]] = (
    BundledDesign(
        id=DesignId("sleepy-puddle"),
        label="Purple puddle",
        filename="01-sleepy-puddle.svg",
        eyes_left_x=41,
        eyes_right_x=59,
        eyes_y=64,
    ),
    BundledDesign(
        id=DesignId("shy-cube"),
        label="Mint cube",
        filename="04-shy-cube.svg",
        eyes_left_x=40,
        eyes_right_x=60,
        eyes_y=69,
    ),
    BundledDesign(
        id=DesignId("jelly-cat"),
        label="Pink cat",
        filename="07-jelly-cat.svg",
        eyes_left_x=41,
        eyes_right_x=59,
        eyes_y=59,
    ),
    BundledDesign(
        id=DesignId("bubble-snail"),
        label="Mint snail",
        filename="14-bubble-snail.svg",
        eyes_left_x=19,
        eyes_right_x=39,
        eyes_y=64,
    ),
    BundledDesign(
        id=DEFAULT_DESIGN_ID,
        label="Gummy seal",
        filename="15-gummy-seal.svg",
        eyes_left_x=36,
        eyes_right_x=54,
        eyes_y=61,
    ),
    BundledDesign(
        id=DesignId("heart-bubble"),
        label="Heart bubble",
        filename="20-heart-bubble.svg",
        eyes_left_x=40,
        eyes_right_x=60,
        eyes_y=51,
    ),
    BundledDesign(
        id=DesignId("jelly-dragon"),
        label="Gold dragon",
        filename="24-jelly-dragon.svg",
        eyes_left_x=40,
        eyes_right_x=60,
        eyes_y=54,
    ),
)

# Deliberately a drawing format, not an embedded document: no scripts, style attributes, arbitrary stylesheets,
# SMIL, hrefs, image loads, foreign content, or filters. defusedxml refuses DTDs and entities; a presentation URL
# can only name a local gradient.
_ELEMENTS: Final[frozenset[str]] = frozenset(
    {
        "svg",
        "g",
        "defs",
        "title",
        "desc",
        "path",
        "circle",
        "ellipse",
        "rect",
        "line",
        "polyline",
        "polygon",
        "linearGradient",
        "radialGradient",
        "stop",
        "style",
    }
)
_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {
        "id",
        "class",
        "viewBox",
        "width",
        "height",
        "x",
        "y",
        "x1",
        "x2",
        "y1",
        "y2",
        "cx",
        "cy",
        "r",
        "rx",
        "ry",
        "d",
        "points",
        "fill",
        "fill-rule",
        "fill-opacity",
        "stroke",
        "stroke-width",
        "stroke-linecap",
        "stroke-linejoin",
        "stroke-opacity",
        "stroke-dasharray",
        "stroke-dashoffset",
        "opacity",
        "offset",
        "stop-color",
        "stop-opacity",
        "gradientUnits",
        "gradientTransform",
        "spreadMethod",
        "fx",
        "fy",
        "transform",
        "role",
        "aria-labelledby",
        "data-mood",
    }
)
_PAINT_ATTRIBUTES: Final[frozenset[str]] = frozenset({"fill", "stroke", "stop-color"})
_LOCAL_PAINT: Final[re.Pattern[str]] = re.compile(r"url\(#[A-Za-z_][A-Za-z0-9_.-]*\)")
_COLOR: Final[re.Pattern[str]] = re.compile(r"(?:#[0-9a-fA-F]{3,8}|[A-Za-z]+|(?:rgb|rgba|hsl|hsla)\([0-9.,% +\-/]+\))")


def parse_design_svg(svg: str) -> Element:
    """Validate passive drawing markup, answering the parsed tree; raises InvalidShellValueError rather than
    repairing unsafe input silently."""
    if len(svg.encode("utf-8")) > MAX_SVG_BYTES:
        raise InvalidShellValueError(f"the design exceeds {MAX_SVG_BYTES // 1024} KiB")
    if "<?" in svg:
        raise InvalidShellValueError("XML processing instructions are not supported in a design")
    try:
        root = fromstring(svg, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except (ParseError, DefusedXmlException) as e:
        raise InvalidShellValueError(f"invalid design SVG: {e}") from e
    if root.tag != f"{{{SVG_NAMESPACE}}}svg" or root.get("viewBox") != REQUIRED_VIEW_BOX:
        raise InvalidShellValueError(f'a design is an SVG in the SVG namespace with viewBox="{REQUIRED_VIEW_BOX}"')
    for element in root.iter():
        tag = element.tag.removeprefix(f"{{{SVG_NAMESPACE}}}")
        if tag not in _ELEMENTS or element.tag != f"{{{SVG_NAMESPACE}}}{tag}":
            raise InvalidShellValueError(f"unsupported design SVG element: {tag}")
        if tag == "svg" and element is not root:
            raise InvalidShellValueError("nested SVG documents are not supported in a design")
        if tag == "style" and (len(element) or (element.text or "").strip() != ANIMATION_CSS.strip()):
            raise InvalidShellValueError(
                "custom CSS is not supported in a design; animate through the jelly-body, jelly-eyes, and "
                "jelly-extra classes"
            )
        for name, value in element.attrib.items():
            if name not in _ATTRIBUTES:
                raise InvalidShellValueError(f"unsupported design SVG attribute: {name}")
            if name in _PAINT_ATTRIBUTES and not (_LOCAL_PAINT.fullmatch(value) or _COLOR.fullmatch(value)):
                raise InvalidShellValueError(f"unsupported design SVG paint: {value}")
    return root


@pure
def bundled_design(design_id: str) -> BundledDesign | None:
    return next((design for design in BUNDLED_DESIGNS if design.id == design_id), None)


def bundled_design_source(design: BundledDesign) -> str:
    return (ASSET_DIRECTORY / design.filename).read_text(encoding="utf-8")


def _apply_bundled_expression(root: Element, design: BundledDesign, is_awake: bool) -> None:
    """The bundled drawings rest with closed eyes and wake with open ones; an original whose eyes are already open
    ellipses keeps its own highlights and proportions."""
    eyes = root.find(f".//{{{SVG_NAMESPACE}}}g[@class='{_EYES_CLASS}']")
    if eyes is None:
        raise InvalidShellValueError(f"bundled design {design.id} has no {_EYES_CLASS} group")
    if is_awake and eyes.find(f"{{{SVG_NAMESPACE}}}ellipse") is not None:
        return
    for child in list(eyes):
        eyes.remove(child)
    if is_awake:
        for x in (design.eyes_left_x, design.eyes_right_x):
            SubElement(
                eyes, f"{{{SVG_NAMESPACE}}}ellipse", {"cx": str(x), "cy": str(design.eyes_y), "rx": "2.1", "ry": "2.7"}
            )
        return
    left, right, y = design.eyes_left_x - 4, design.eyes_right_x - 4, design.eyes_y
    SubElement(
        eyes,
        f"{{{SVG_NAMESPACE}}}path",
        {
            "d": f"M{left} {y}q4 6 8 0M{right} {y}q4 6 8 0",
            "fill": "none",
            "stroke": "#382643",
            "stroke-width": "2.6",
            "stroke-linecap": "round",
        },
    )


def _apply_bundled_motion(root: Element, design: BundledDesign) -> None:
    """The seal paddles its flippers; every other bundled design has a motion of its own in the shared sheet."""
    if design.id == DEFAULT_DESIGN_ID:
        body = root.find(f"{{{SVG_NAMESPACE}}}g[@class='{_BODY_CLASS}']")
        if body is None:
            raise InvalidShellValueError(f"bundled design {design.id} has no {_BODY_CLASS} group")
        flippers = body.find(f"{{{SVG_NAMESPACE}}}path[2]")
        if flippers is None:
            raise InvalidShellValueError(f"bundled design {design.id} has no flippers to paddle")
        flippers.set("class", _FLIPPERS_CLASS)
        SubElement(root, _STYLE_TAG).text = _SEAL_MOTION_CSS
        return
    root.set("class", str(design.id))
    SubElement(root, _STYLE_TAG).text = _BUILTIN_MOTION_CSS


def render_design_svg(svg: str, mood: AvatarMood, is_preview: bool, design_id: str) -> str:
    """One isolated image of a design wearing ``mood``: the source stays untouched, the copy carries the mood, the
    shared stylesheet, and (for a bundled design) its expression and motion; a preview holds every pose still."""
    root = parse_design_svg(svg)
    root.set("data-mood", mood.value)
    styles = list(root.iter(_STYLE_TAG))
    if not styles:
        styles = [SubElement(root, _STYLE_TAG)]
    for style in styles:
        style.text = ANIMATION_CSS
    design = bundled_design(design_id)
    if design is not None:
        is_working = mood is AvatarMood.WORKING and not is_preview
        _apply_bundled_expression(root, design, is_awake=is_working)
        if is_working:
            _apply_bundled_motion(root, design)
    if is_preview:
        SubElement(root, _STYLE_TAG).text = _PREVIEW_CSS
    return tostring(root, encoding="unicode")
