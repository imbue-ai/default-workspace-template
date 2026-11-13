"""The workspace color (docs/system/blueprint/brand-design-system/): the one color Imbue Studio lets a user pick for
a workspace, and the keys the standard look's ramps are computed from when it is not Imbue's own.

The picked color is the primary key, exactly. The neutral key sits at the color's hue + 150 degrees and the accent
at + 210 (its two split complements), and the status hues lean toward it. Every key but the neutral is the lightest
color at its hue and chroma that white text still reads on, so a key is always a surface white text can sit on;
the text on the picked color itself is whichever of near-black or near-white reads stronger on it (APCA's
polarity). The colour math is OKLCH: Bjorn Ottosson's OKLab matrices, gamut mapping by lowering chroma as CSS Color
4 does, WCAG 2 contrast from the real luminance, and APCA-W3 0.1.9 for polarity."""

import json
import math
import re
from pathlib import Path
from typing import Final
from urllib.parse import quote

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from pydantic import Field, ValidationError

from workspace_themes.contract import STUDIO_WALLPAPER_URL

# Imbue's own primary, Courage (Red 11). A workspace wearing it gets the standard look's hand-picked keys rather
# than ones derived from it.
IMBUE_PRIMARY: Final[str] = "#492222"

_HEX_PATTERN: Final = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_NEUTRAL_KEY_LIGHTNESS: Final[float] = 0.73
_NEUTRAL_KEY_CHROMA: Final[float] = 0.016
# The accent's chroma at least this, so it reads as a color even beside a gray seed.
_ACCENT_CHROMA_FLOOR: Final[float] = 0.1
# Each status role's own hue and chroma, before it leans toward the seed.
_STATUS_HUE_AND_CHROMA: Final[tuple[tuple[str, float, float], ...]] = (
    ("success", 155.0, 0.14),
    ("warn", 60.0, 0.15),
    ("danger", 28.0, 0.19),
)
_HARMONIZE_MAX_DEGREES: Final[float] = 15.0
# What white text on a role's key must reach (WCAG 2), as on Imbue's own keys.
ON_KEY_CONTRAST: Final[float] = 4.5
# APCA's floor for body text: a tinted text color under it falls back to pure black or white.
_TINTED_TEXT_MIN_LC: Final[float] = 60.0
_BISECTION_STEPS: Final[int] = 24
_GAMUT_EPSILON: Final[float] = 0.0005


class Oklch(FrozenModel):
    """A color in OKLCH: L from 0 (black) to 1 (white), C from 0 to about 0.37 inside sRGB, h in degrees."""

    lightness: float = Field(description="OKLCH L, 0 to 1")
    chroma: float = Field(description="OKLCH C")
    hue: float = Field(description="OKLCH h, degrees")


class MeasuredColor(FrozenModel):
    """An sRGB color and where it sits in OKLCH."""

    hex: str = Field(description="#rrggbb")
    oklch: Oklch = Field(description="The color in OKLCH")


class WorkspaceKeys(FrozenModel):
    """The key colors a workspace color stands for, each a CSS color, plus the text that reads on its primary."""

    neutral: str = Field(description="The neutral ramp's key")
    primary: str = Field(
        description="The primary ramp's key: the workspace color itself"
    )
    accent: str = Field(description="The accent ramp's key")
    success: str = Field(description="The success ramp's key")
    warn: str = Field(description="The warn ramp's key")
    danger: str = Field(description="The danger ramp's key")
    primary_content: str = Field(description="The text color on the primary surface")


@pure
def normalize_hex(value: str) -> str | None:
    """The canonical lowercase #rrggbb for a 3- or 6-digit hex color, or None when it is not one."""
    match = _HEX_PATTERN.match(value.strip())
    if match is None:
        return None
    body = match.group(1).lower()
    if len(body) == 3:
        body = "".join(character * 2 for character in body)
    return f"#{body}"


@pure
def is_imbue(hex_color: str) -> bool:
    return hex_color.lower() == IMBUE_PRIMARY


@pure
def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


@pure
def _to_linear(channel: float) -> float:
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


@pure
def _from_linear(channel: float) -> float:
    return (
        12.92 * channel
        if channel <= 0.0031308
        else 1.055 * channel ** (1 / 2.4) - 0.055
    )


@pure
def _channels(hex_color: str) -> tuple[int, int, int]:
    value = int(hex_color[1:], 16)
    return (value >> 16) & 255, (value >> 8) & 255, value & 255


@pure
def _linear_from_hex(hex_color: str) -> tuple[float, float, float]:
    red, green, blue = _channels(hex_color)
    return _to_linear(red / 255), _to_linear(green / 255), _to_linear(blue / 255)


@pure
def _hex_from_linear(rgb: tuple[float, float, float]) -> str:
    red, green, blue = (round(_clamp01(_from_linear(channel)) * 255) for channel in rgb)
    return f"#{red:02x}{green:02x}{blue:02x}"


@pure
def _oklab_from_linear(rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    red, green, blue = rgb
    long = math.cbrt(0.4122214708 * red + 0.5363325363 * green + 0.0514459929 * blue)
    medium = math.cbrt(0.2119034982 * red + 0.6806995451 * green + 0.1073969566 * blue)
    short = math.cbrt(0.0883024619 * red + 0.2817188376 * green + 0.6299787005 * blue)
    return (
        0.2104542553 * long + 0.793617785 * medium - 0.0040720468 * short,
        1.9779984951 * long - 2.428592205 * medium + 0.4505937099 * short,
        0.0259040371 * long + 0.7827717662 * medium - 0.808675766 * short,
    )


@pure
def _linear_from_oklch(color: Oklch) -> tuple[float, float, float]:
    radians = math.radians(color.hue)
    a = color.chroma * math.cos(radians)
    b = color.chroma * math.sin(radians)
    long = (color.lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
    medium = (color.lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
    short = (color.lightness - 0.0894841775 * a - 1.291485548 * b) ** 3
    return (
        4.0767416621 * long - 3.3077115913 * medium + 0.2309699292 * short,
        -1.2684380046 * long + 2.6097574011 * medium - 0.3413193965 * short,
        -0.0041960863 * long - 0.7034186147 * medium + 1.707614701 * short,
    )


@pure
def _is_in_gamut(rgb: tuple[float, float, float]) -> bool:
    return all(-_GAMUT_EPSILON <= channel <= 1 + _GAMUT_EPSILON for channel in rgb)


@pure
def measure(hex_color: str) -> MeasuredColor:
    lightness, a, b = _oklab_from_linear(_linear_from_hex(hex_color))
    hue = (math.degrees(math.atan2(b, a)) + 360) % 360
    return MeasuredColor(
        hex=hex_color,
        oklch=Oklch(lightness=lightness, chroma=math.hypot(a, b), hue=hue),
    )


@pure
def max_chroma(lightness: float, hue: float) -> float:
    """The most chroma ``hue`` can carry at ``lightness`` inside sRGB."""
    if lightness <= 0 or lightness >= 1:
        return 0.0
    low, high = 0.0, 0.4
    for _ in range(_BISECTION_STEPS):
        middle = (low + high) / 2
        if _is_in_gamut(
            _linear_from_oklch(Oklch(lightness=lightness, chroma=middle, hue=hue))
        ):
            low = middle
        else:
            high = middle
    return low


@pure
def fit(color: Oklch) -> MeasuredColor:
    """The color at its lightness and hue with as much of its chroma as sRGB allows."""
    lightness = _clamp01(color.lightness)
    chroma = min(color.chroma, max_chroma(lightness, color.hue))
    return measure(
        _hex_from_linear(
            _linear_from_oklch(Oklch(lightness=lightness, chroma=chroma, hue=color.hue))
        )
    )


@pure
def luminance(hex_color: str) -> float:
    red, green, blue = _linear_from_hex(hex_color)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


@pure
def contrast(first: str, second: str) -> float:
    """WCAG 2's contrast ratio between two colors."""
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


@pure
def solve_lightness(
    background: str, hue: float, chroma: float, ratio: float, is_darker: bool
) -> MeasuredColor:
    """Walking from the background's own lightness toward black (``is_darker``) or white, the quietest color at
    ``hue`` and ``chroma`` that reaches ``ratio`` against ``background``; the end of the walk if none does."""
    start = measure(background).oklch.lightness
    low, high = (0.0, start) if is_darker else (start, 1.0)
    end = 0.0 if is_darker else 1.0

    def is_passing(lightness: float) -> bool:
        return (
            contrast(
                fit(Oklch(lightness=lightness, chroma=chroma, hue=hue)).hex, background
            )
            >= ratio
        )

    if not is_passing(end):
        return fit(Oklch(lightness=end, chroma=chroma, hue=hue))
    for _ in range(_BISECTION_STEPS):
        middle = (low + high) / 2
        if is_passing(middle) == is_darker:
            low = middle
        else:
            high = middle
    return fit(Oklch(lightness=low if is_darker else high, chroma=chroma, hue=hue))


@pure
def turn(hue: float, degrees: float) -> float:
    return ((hue + degrees) % 360 + 360) % 360


@pure
def harmonize(
    hue: float, toward: float, max_degrees: float = _HARMONIZE_MAX_DEGREES
) -> float:
    """``hue`` nudged toward ``toward`` by half the gap between them, at most ``max_degrees``."""
    difference = (toward - hue + 540) % 360 - 180
    return turn(
        hue,
        math.copysign(min(abs(difference) / 2, max_degrees), difference)
        if difference
        else 0.0,
    )


@pure
def apca(text: str, background: str) -> float:
    """APCA lightness contrast (Lc), text on background: about +-100 at most, negative for light text on dark."""

    def screen_luminance(hex_color: str) -> float:
        red, green, blue = (channel / 255 for channel in _channels(hex_color))
        value = 0.2126729 * red**2.4 + 0.7151522 * green**2.4 + 0.072175 * blue**2.4
        return value + (0.022 - value) ** 1.414 if value < 0.022 else value

    text_y = screen_luminance(text)
    background_y = screen_luminance(background)
    if abs(background_y - text_y) < 0.0005:
        return 0.0
    if background_y > text_y:
        score = (background_y**0.56 - text_y**0.57) * 1.14
        return 0.0 if score < 0.1 else (score - 0.027) * 100
    score = (background_y**0.65 - text_y**0.62) * 1.14
    return 0.0 if score > -0.1 else (score + 0.027) * 100


@pure
def css_oklch(color: Oklch) -> str:
    """The oklch() a stylesheet carries for a color, rounded."""
    hue = "0" if color.chroma < 0.0005 else f"{color.hue:.1f}"
    return f"oklch({color.lightness * 100:.1f}% {color.chroma:.3f} {hue})"


@pure
def readable_on(background: str, hue: float, chroma: float) -> MeasuredColor:
    """Text on ``background``: near black or near white (whichever APCA says reads stronger), lightly tinted with the
    seed's hue; pure black or white if the tint drops it under APCA's floor for body text."""
    is_toward_black = abs(apca("#000000", background)) > abs(
        apca("#ffffff", background)
    )
    tinted = fit(
        Oklch(
            lightness=0.22 if is_toward_black else 0.985,
            chroma=min(chroma, 0.02),
            hue=hue,
        )
    )
    if abs(apca(tinted.hex, background)) >= _TINTED_TEXT_MIN_LC:
        return tinted
    return measure("#000000" if is_toward_black else "#ffffff")


@pure
def _key_for(hue: float, chroma: float) -> str:
    return solve_lightness("#ffffff", hue, chroma, ON_KEY_CONTRAST, True).hex


@pure
def derive_workspace_keys(hex_color: str) -> WorkspaceKeys | None:
    """The keys a workspace color stands for, or None when it is Imbue's own (the standard look's keys stand)."""
    if is_imbue(hex_color):
        return None
    seed = measure(hex_color).oklch
    status = {
        name: _key_for(harmonize(hue, seed.hue), chroma)
        for name, hue, chroma in _STATUS_HUE_AND_CHROMA
    }
    return WorkspaceKeys(
        neutral=f"oklch({_NEUTRAL_KEY_LIGHTNESS:.0%} {_NEUTRAL_KEY_CHROMA} {turn(seed.hue, 150):.1f})",
        primary=hex_color,
        accent=_key_for(turn(seed.hue, 210), max(seed.chroma, _ACCENT_CHROMA_FLOOR)),
        success=status["success"],
        warn=status["warn"],
        danger=status["danger"],
        primary_content=readable_on(hex_color, seed.hue, seed.chroma).hex,
    )


@pure
def render_workspace_color_css(keys: WorkspaceKeys | None, revision: str) -> str:
    """The stylesheet every app serves as /_static/workspace_color.css: the keys over the standard look's (none for
    Imbue's own color), and the standard wallpaper at the color's revision. The wallpaper follows the primary."""
    wallpaper = f'  --desk-studio-wallpaper: url("{STUDIO_WALLPAPER_URL}?v={quote(revision)}");\n'
    if keys is None:
        return f"/* The workspace wears Imbue's own color: the standard keys stand. */\n:root {{\n{wallpaper}}}\n"
    return (
        ":root {\n"
        f"  --c-neutral-key: {keys.neutral};\n"
        f"  --c-primary-key: {keys.primary};\n"
        f"  --c-accent-key: {keys.accent};\n"
        f"  --c-success-key: {keys.success};\n"
        f"  --c-warn-key: {keys.warn};\n"
        f"  --c-danger-key: {keys.danger};\n"
        "  --c-wallpaper-key: var(--c-primary-key);\n"
        f"  --c-primary-content: {keys.primary_content};\n"
        f"{wallpaper}"
        "}\n"
    )


class WorkspaceColorRecord(FrozenModel):
    """The workspace color the shell last read, as kept in WORKSPACE_COLOR_STATE_PATH."""

    hex: str | None = Field(
        description="The color as #rrggbb, or None when the workspace has none (Imbue's own)"
    )


@pure
def workspace_color_revision(record: WorkspaceColorRecord) -> str:
    """What changes exactly when the served stylesheet does: the color, or Imbue's own."""
    return (
        record.hex if record.hex is not None and not is_imbue(record.hex) else "imbue"
    )


def read_workspace_color(state_path: Path) -> WorkspaceColorRecord:
    """The kept workspace color; no color (Imbue's own) when nothing is kept or the file cannot be read."""
    try:
        record = WorkspaceColorRecord.model_validate(
            json.loads(state_path.read_text(encoding="utf-8"))
        )
    except (OSError, ValueError, ValidationError):
        return WorkspaceColorRecord(hex=None)
    normalized = normalize_hex(record.hex) if record.hex is not None else None
    return WorkspaceColorRecord(hex=normalized)


def write_workspace_color(state_path: Path, record: WorkspaceColorRecord) -> None:
    """Keeps a workspace color, replacing the file whole so a reader never sees half of one."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path = state_path.with_suffix(".partial")
    partial_path.write_text(record.model_dump_json(), encoding="utf-8")
    partial_path.replace(state_path)


@pure
def workspace_color_css(record: WorkspaceColorRecord) -> str:
    keys = None if record.hex is None else derive_workspace_keys(record.hex)
    return render_workspace_color_css(keys, workspace_color_revision(record))
