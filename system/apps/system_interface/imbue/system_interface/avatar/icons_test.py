import pytest

from imbue.system_interface.avatar.designs import BUNDLED_DESIGNS
from imbue.system_interface.avatar.designs import bundled_design_source
from imbue.system_interface.avatar.icons import MAX_ICON_SIZE
from imbue.system_interface.avatar.icons import MIN_ICON_SIZE
from imbue.system_interface.avatar.icons import render_design_icon_png
from imbue.system_interface.avatar.testing import EMPTY_DESIGN_SVG
from imbue.system_interface.avatar.testing import MINIMAL_DESIGN_SVG
from imbue.system_interface.avatar.testing import png_size
from imbue.system_interface.avatar.testing import png_top_left_rgba
from imbue.system_interface.shell.errors import InvalidShellValueError

# The shell's page colour (base.css ``--c-bg``), opaque.
_PAGE_COLOR_RGBA = (0xFA, 0xFA, 0xF8, 0xFF)


def test_every_bundled_design_draws_a_distinct_icon_on_an_opaque_page_coloured_tile() -> None:
    """A home screen fills a transparent pixel with black, so the tile's corner is the page colour at full opacity,
    and each design draws something of its own on it rather than the bare tile."""
    bare = render_design_icon_png(EMPTY_DESIGN_SVG, "blank", 64)
    icons = {
        str(design.id): render_design_icon_png(bundled_design_source(design), str(design.id), 64)
        for design in BUNDLED_DESIGNS
    }
    assert png_top_left_rgba(bare) == _PAGE_COLOR_RGBA
    for icon in icons.values():
        assert png_size(icon) == (64, 64)
        assert png_top_left_rgba(icon) == _PAGE_COLOR_RGBA
        assert icon != bare
    assert len(set(icons.values())) == len(BUNDLED_DESIGNS)


def test_a_registered_design_draws_its_icon_and_a_size_off_the_bounds_is_refused() -> None:
    icon = render_design_icon_png(MINIMAL_DESIGN_SVG, "mine", MAX_ICON_SIZE)
    assert png_size(icon) == (MAX_ICON_SIZE, MAX_ICON_SIZE)
    assert icon != render_design_icon_png(EMPTY_DESIGN_SVG, "blank", MAX_ICON_SIZE)
    for size in (MIN_ICON_SIZE - 1, MAX_ICON_SIZE + 1):
        with pytest.raises(InvalidShellValueError):
            render_design_icon_png(MINIMAL_DESIGN_SVG, "mine", size)
