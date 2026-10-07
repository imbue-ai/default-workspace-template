import math
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from imbue.imbue_common.pure import pure
from PIL import Image, ImageDraw, ImageStat, UnidentifiedImageError

from workspace_themes.contract import GENERATED_ICONS_DIRECTORY, ICONS_DIRECTORY
from workspace_themes.data_types import ThemeEntry, ThemeIconSpec
from workspace_themes.errors import IconImageError
from workspace_themes.primitives import (
    HexColor,
    IconBackground,
    IconFormat,
    IconRendering,
    ThemeSource,
)

# A pixel at least this opaque counts as part of the icon.
OPAQUE_ALPHA_THRESHOLD: Final[int] = 128
# How close to the corner color a pixel must be, per channel distance, to be taken for flat background.
BACKGROUND_TOLERANCE: Final[int] = 48
# The clear border left around a fitted icon, as a share of its side.
FIT_MARGIN_FRACTION: Final[float] = 1 / 16
# An icon covering less than this share of its canvas is too faint to read; more than this has no shape.
MIN_COVERAGE: Final[float] = 0.06
MAX_TRANSPARENT_COVERAGE: Final[float] = 0.97
# The least spread of brightness inside an icon, at 16 pixels, for its shape to read at all.
MIN_LUMINANCE_SPREAD: Final[float] = 12.0
LEGIBILITY_CHECK_SIZE: Final[int] = 16
TRANSPARENT_PIXEL: Final[tuple[int, int, int, int]] = (0, 0, 0, 0)
OPAQUE_FILL: Final[tuple[int, int, int, int]] = (255, 255, 255, 255)
GRID_TRANSPARENT_CHARACTERS: Final[frozenset[str]] = frozenset({".", " "})
# A preview is enlarged by a whole factor to at least this side, so a small icon can be judged by eye.
PREVIEW_MIN_SIZE: Final[int] = 256
# The checkerboard a preview shows behind clear pixels, so clear and white read apart.
PREVIEW_CHECKER_COLORS: Final[tuple[tuple[int, int, int, int], ...]] = (
    (204, 204, 204, 255),
    (240, 240, 240, 255),
)


def load_icon_image(path: Path) -> Image.Image:
    """Raises IconImageError when the file is not an image Pillow reads."""
    try:
        with Image.open(path) as opened:
            return opened.convert("RGBA")
    except (OSError, UnidentifiedImageError) as error:
        raise IconImageError(f"{path} cannot be read as an image: {error}") from error


@pure
def rgba_pixels(image: Image.Image) -> list[tuple[int, int, int, int]]:
    """Every pixel of the image as red, green, blue, and alpha, row by row."""
    return [
        (int(pixel[0]), int(pixel[1]), int(pixel[2]), int(pixel[3]))
        for pixel in image.convert("RGBA").get_flattened_data()
    ]


@pure
def _alpha_at(image: Image.Image, point: tuple[int, int]) -> int:
    value = image.getchannel("A").getpixel(point)
    return int(value) if isinstance(value, (int, float)) else 0


@pure
def _is_corner_opaque(image: Image.Image) -> bool:
    width, height = image.size
    corners = [(0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)]
    return all(_alpha_at(image, corner) >= OPAQUE_ALPHA_THRESHOLD for corner in corners)


def _clear_flat_background(image: Image.Image) -> Image.Image:
    """Clear the flat color a model painted behind the icon: everything like it that reaches the image's edge."""
    cleared = image.copy()
    width, height = cleared.size
    step = max(1, min(width, height) // 32)
    edge_points = [
        *((x, 0) for x in range(0, width, step)),
        *((x, height - 1) for x in range(0, width, step)),
        *((0, y) for y in range(0, height, step)),
        *((width - 1, y) for y in range(0, height, step)),
    ]
    for point in edge_points:
        if _alpha_at(cleared, point) == 0:
            continue
        ImageDraw.floodfill(
            cleared, point, TRANSPARENT_PIXEL, thresh=BACKGROUND_TOLERANCE
        )
    return cleared


def _trim_and_square(image: Image.Image) -> Image.Image:
    mask = image.getchannel("A").point(
        lambda alpha: 255 if alpha >= OPAQUE_ALPHA_THRESHOLD else 0
    )
    box = mask.getbbox()
    if box is None:
        raise IconImageError("the image is empty once its background is cleared")
    trimmed = image.crop(box)
    side = max(trimmed.size)
    margin = max(1, round(side * FIT_MARGIN_FRACTION))
    canvas = Image.new(
        "RGBA", (side + 2 * margin, side + 2 * margin), TRANSPARENT_PIXEL
    )
    canvas.paste(
        trimmed,
        ((canvas.width - trimmed.width) // 2, (canvas.height - trimmed.height) // 2),
    )
    return canvas


def _scale_to_canvas(image: Image.Image, spec: ThemeIconSpec) -> Image.Image:
    is_upscale = image.width < spec.size
    if spec.rendering == IconRendering.PIXELATED:
        resample = Image.Resampling.NEAREST if is_upscale else Image.Resampling.BOX
    else:
        resample = Image.Resampling.LANCZOS
    return image.resize((spec.size, spec.size), resample=resample)


@pure
def _nearest_palette_color(
    rgb: tuple[int, int, int], palette: Sequence[tuple[int, int, int]]
) -> tuple[int, int, int]:
    return min(
        palette,
        key=lambda color: sum((a - b) ** 2 for a, b in zip(rgb, color, strict=True)),
    )


def _apply_palette(image: Image.Image, palette: Sequence[HexColor]) -> Image.Image:
    colors = [color.as_rgb() for color in palette]
    mapped_pixels = [
        TRANSPARENT_PIXEL
        if alpha == 0
        else (*_nearest_palette_color((red, green, blue), colors), alpha)
        for red, green, blue, alpha in rgba_pixels(image)
    ]
    mapped = Image.new("RGBA", image.size)
    mapped.putdata(mapped_pixels)
    return mapped


def _limit_colors(image: Image.Image, max_colors: int) -> Image.Image:
    alpha = image.getchannel("A")
    reduced = (
        image.convert("RGB")
        .quantize(colors=max_colors, dither=Image.Dither.NONE)
        .convert("RGBA")
    )
    reduced.putalpha(alpha)
    return reduced


def fit_icon_image(image: Image.Image, spec: ThemeIconSpec) -> Image.Image:
    """Force a drawn candidate onto the theme's limits; raises IconImageError when nothing of it is left."""
    working = image.convert("RGBA")
    if spec.background == IconBackground.TRANSPARENT and _is_corner_opaque(working):
        working = _clear_flat_background(working)
    working = _scale_to_canvas(_trim_and_square(working), spec)
    if spec.rendering == IconRendering.PIXELATED:
        working.putalpha(
            working.getchannel("A").point(
                lambda alpha: 255 if alpha >= OPAQUE_ALPHA_THRESHOLD else 0
            )
        )
    if spec.background == IconBackground.OPAQUE:
        filled = Image.new("RGBA", working.size, OPAQUE_FILL)
        filled.alpha_composite(working)
        working = filled
    if spec.palette:
        working = _apply_palette(working, spec.palette)
    elif spec.max_colors is not None:
        working = _limit_colors(working, spec.max_colors)
    return working


@pure
def _opaque_colors(image: Image.Image) -> set[tuple[int, int, int]]:
    return {
        (red, green, blue)
        for red, green, blue, alpha in rgba_pixels(image)
        if alpha >= OPAQUE_ALPHA_THRESHOLD
    }


def _luminance_spread_at_small_size(image: Image.Image) -> float:
    small = image.resize(
        (LEGIBILITY_CHECK_SIZE, LEGIBILITY_CHECK_SIZE), resample=Image.Resampling.BOX
    )
    backdrop = Image.new("RGBA", small.size, (128, 128, 128, 255))
    backdrop.alpha_composite(small)
    return ImageStat.Stat(backdrop.convert("L")).stddev[0]


def check_icon_image(image: Image.Image, spec: ThemeIconSpec) -> list[str]:
    """Every way a png icon misses the theme's limits (docs/system/blueprint/workspace-themes/, section 6.2)."""
    problems: list[str] = []
    rgba = image.convert("RGBA")
    if rgba.size != (spec.size, spec.size):
        return [
            f"the icon is {rgba.width}x{rgba.height}; the theme's icons are {spec.size}x{spec.size}"
        ]
    alphas = [alpha for _, _, _, alpha in rgba_pixels(rgba)]
    coverage = sum(1 for alpha in alphas if alpha >= OPAQUE_ALPHA_THRESHOLD) / len(
        alphas
    )
    if coverage < MIN_COVERAGE:
        problems.append(
            f"the icon covers {coverage:.0%} of its canvas, too little to read"
        )
    if spec.background == IconBackground.TRANSPARENT:
        if _is_corner_opaque(rgba):
            problems.append(
                "the theme's icons have clear corners, but this one's are painted"
            )
        if coverage > MAX_TRANSPARENT_COVERAGE:
            problems.append(
                "the icon fills its whole canvas; the theme's icons stand on a clear background"
            )
    if spec.rendering == IconRendering.PIXELATED and any(
        0 < alpha < 255 for alpha in alphas
    ):
        problems.append("a pixelated theme's icons have no partly transparent pixels")
    colors = _opaque_colors(rgba)
    if spec.palette:
        allowed = {color.as_rgb() for color in spec.palette}
        stray = sorted(colors - allowed)
        if stray:
            problems.append(
                f"{len(stray)} colors are outside the theme's palette, such as {stray[0]}"
            )
    if spec.max_colors is not None and len(colors) > spec.max_colors:
        problems.append(
            f"the icon uses {len(colors)} colors; the theme allows {spec.max_colors}"
        )
    if _luminance_spread_at_small_size(rgba) < MIN_LUMINANCE_SPREAD:
        problems.append(
            f"at {LEGIBILITY_CHECK_SIZE} pixels the icon is one flat tone; give it contrast"
        )
    return problems


def render_pixel_grid(
    grid_text: str, color_by_character: Mapping[str, HexColor]
) -> Image.Image:
    """Draw a pixel grid an agent wrote, one character per pixel; '.' and ' ' are clear."""
    rows = [line.rstrip("\n") for line in grid_text.strip("\n").splitlines()]
    if not rows:
        raise IconImageError("the grid has no rows")
    width = max(len(row) for row in rows)
    image = Image.new("RGBA", (width, len(rows)), TRANSPARENT_PIXEL)
    for y, row in enumerate(rows):
        for x, character in enumerate(row):
            if character in GRID_TRANSPARENT_CHARACTERS:
                continue
            color = color_by_character.get(character)
            if color is None:
                raise IconImageError(
                    f"row {y + 1} uses {character!r}, which the legend gives no color"
                )
            image.putpixel((x, y), (*color.as_rgb(), 255))
    return image


@pure
def icon_install_path(
    entry: ThemeEntry, repo_root: Path, app_name: str, icon_format: IconFormat
) -> Path:
    """Where an icon made in this workspace goes: a workspace theme's own folder, or data/ for a built-in theme."""
    file_name = f"{app_name}.{icon_format}"
    if entry.source == ThemeSource.WORKSPACE:
        return entry.folder / ICONS_DIRECTORY / file_name
    return (
        repo_root / GENERATED_ICONS_DIRECTORY / entry.id / ICONS_DIRECTORY / file_name
    )


def install_icon_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


@pure
def pixel_scale_hint(spec: ThemeIconSpec) -> int:
    """How many times larger to ask a model to draw, so its strokes survive the scale down to the canvas."""
    return max(1, math.ceil(512 / spec.size))


@pure
def preview_icon_image(image: Image.Image, spec: ThemeIconSpec) -> Image.Image:
    """The icon enlarged by a whole factor over a checkerboard, as the shell would scale it."""
    factor = max(1, math.ceil(PREVIEW_MIN_SIZE / image.width))
    resample = (
        Image.Resampling.NEAREST
        if spec.rendering == IconRendering.PIXELATED
        else Image.Resampling.LANCZOS
    )
    enlarged = image.convert("RGBA").resize(
        (image.width * factor, image.height * factor), resample=resample
    )
    backdrop = Image.new("RGBA", enlarged.size)
    cell = max(4, factor)
    for y in range(0, enlarged.height, cell):
        for x in range(0, enlarged.width, cell):
            color = PREVIEW_CHECKER_COLORS[(x // cell + y // cell) % 2]
            backdrop.paste(color, (x, y, x + cell, y + cell))
    return Image.alpha_composite(backdrop, enlarged)
