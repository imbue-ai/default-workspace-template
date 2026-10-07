from pathlib import Path

import pytest
from PIL import Image

from workspace_themes.data_types import ThemeIconSpec
from workspace_themes.errors import IconImageError
from workspace_themes.icons import (
    check_icon_image,
    fit_icon_image,
    preview_icon_image,
    render_pixel_grid,
    rgba_pixels,
)
from workspace_themes.primitives import (
    HexColor,
    IconBackground,
    IconDerivation,
    IconFormat,
    IconRendering,
    ThemeRelativePath,
)
from workspace_themes.validation import check_svg_icon


def _spec(
    palette: tuple[str, ...] = (),
    max_colors: int | None = None,
    background: IconBackground = IconBackground.TRANSPARENT,
) -> ThemeIconSpec:
    return ThemeIconSpec(
        guide=ThemeRelativePath("icons/guide.md"),
        format=IconFormat.PNG,
        size=32,
        rendering=IconRendering.PIXELATED,
        background=background,
        palette=tuple(HexColor(color) for color in palette),
        max_colors=max_colors,
        derive=IconDerivation.QUANTIZE,
    )


def _model_drawing() -> Image.Image:
    """What an image model hands back: a large picture of a red disc with a dark ring, on a flat light background."""
    image = Image.new("RGBA", (512, 512), (240, 240, 235, 255))
    for y in range(512):
        for x in range(512):
            distance = ((x - 256) ** 2 + (y - 240) ** 2) ** 0.5
            if distance < 150:
                image.putpixel((x, y), (200, 30, 30, 255))
            elif distance < 180:
                image.putpixel((x, y), (20, 20, 20, 255))
    return image


def test_a_model_s_drawing_is_fitted_onto_a_one_bit_theme(tmp_path: Path) -> None:
    spec = _spec(palette=("#000000", "#ffffff"))

    fitted = fit_icon_image(_model_drawing(), spec)

    assert fitted.size == (32, 32)
    colors = {
        (red, green, blue) for red, green, blue, alpha in rgba_pixels(fitted) if alpha
    }
    assert colors <= {(0, 0, 0), (255, 255, 255)}
    assert rgba_pixels(fitted)[0][3] == 0
    assert check_icon_image(fitted, spec) == []


def test_a_model_s_drawing_is_limited_to_the_theme_s_color_count() -> None:
    spec = _spec(max_colors=4)

    fitted = fit_icon_image(_model_drawing(), spec)

    assert check_icon_image(fitted, spec) == []


def test_an_opaque_background_is_drawn_in_the_theme_s_palette() -> None:
    spec = _spec(palette=("#000000", "#c81e1e"), background=IconBackground.OPAQUE)

    fitted = fit_icon_image(_model_drawing(), spec)

    assert check_icon_image(fitted, spec) == []


def test_an_icon_that_misses_the_limits_is_told_why() -> None:
    spec = _spec(palette=("#000000", "#ffffff"))
    wrong_size = Image.new("RGBA", (48, 48), (0, 0, 0, 255))
    painted = Image.new("RGBA", (32, 32), (255, 0, 0, 255))

    assert check_icon_image(wrong_size, spec) == [
        "the icon is 48x48; the theme's icons are 32x32"
    ]
    problems = check_icon_image(painted, spec)
    assert (
        "the theme's icons have clear corners, but this one's are painted" in problems
    )
    assert any("outside the theme's palette" in problem for problem in problems)
    assert any("one flat tone" in problem for problem in problems)


def test_an_empty_drawing_cannot_be_fitted() -> None:
    with pytest.raises(IconImageError, match="empty"):
        fit_icon_image(Image.new("RGBA", (64, 64), (255, 255, 255, 255)), _spec())


def test_a_pixel_grid_an_agent_writes_is_drawn_one_character_per_pixel() -> None:
    grid = "..kk..\n.kwwk.\n..kk..\n"

    image = render_pixel_grid(
        grid, {"k": HexColor("#000000"), "w": HexColor("#FFFFFF")}
    )

    assert image.size == (6, 3)
    assert image.getpixel((2, 0)) == (0, 0, 0, 255)
    assert image.getpixel((2, 1)) == (255, 255, 255, 255)
    assert image.getpixel((0, 0)) == (0, 0, 0, 0)
    with pytest.raises(IconImageError, match="'x'"):
        render_pixel_grid("kx", {"k": HexColor("#000000")})


def test_an_svg_icon_must_be_svg_that_scales(tmp_path: Path) -> None:
    good = tmp_path / "good.svg"
    good.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 216 216"/>',
        encoding="utf-8",
    )
    fixed = tmp_path / "fixed.svg"
    fixed.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="216"/>', encoding="utf-8"
    )
    broken = tmp_path / "broken.svg"
    broken.write_text("<svg", encoding="utf-8")

    assert check_svg_icon(good) == []
    assert check_svg_icon(fixed) == ["the icon has no viewBox, so it cannot scale"]
    assert check_svg_icon(broken)[0].startswith("the icon is not well-formed SVG")


def test_an_svg_icon_may_not_run_script_or_reach_outside_itself(
    tmp_path: Path,
) -> None:
    safe = tmp_path / "safe.svg"
    safe.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 8 8">'
        '<defs><path id="p" d="M0 0h8v8z"/></defs><use href="#p"/><use xlink:href="#p"/></svg>',
        encoding="utf-8",
    )
    unsafe = tmp_path / "unsafe.svg"
    unsafe.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 8 8" '
        'onload="alert(1)"><script>alert(2)</script><foreignObject/>'
        '<image href="https://example.com/a.png"/><use xlink:href="other.svg#p"/></svg>',
        encoding="utf-8",
    )

    assert check_svg_icon(safe) == []
    assert check_svg_icon(unsafe) == [
        "the icon's <svg> has an event handler, onload, which an icon may not",
        "the icon has a <script> element, which an icon may not",
        "the icon has a <foreignobject> element, which an icon may not",
        "the icon's <image> href points at 'https://example.com/a.png'; an icon refers only to itself, by #fragment",
        "the icon's <use> href points at 'other.svg#p'; an icon refers only to itself, by #fragment",
    ]


def test_a_preview_enlarges_each_pixel_whole_over_a_checkerboard() -> None:
    icon = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    icon.putpixel((0, 0), (255, 0, 0, 255))

    preview = preview_icon_image(icon, _spec())

    assert preview.size == (256, 256)
    # The one red pixel fills an 8 by 8 block, untouched by smoothing.
    assert {preview.getpixel((x, y)) for x in range(8) for y in range(8)} == {
        (255, 0, 0, 255)
    }
    # The clear pixels show the checkerboard, in both of its colors.
    assert len({preview.getpixel((x, 100)) for x in range(8, 256)}) == 2
