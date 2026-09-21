"""Tests for the design markup contract (docs/system/avatar-designs.md) and the rendered image."""

import pytest
from defusedxml.ElementTree import fromstring

from imbue.system_interface.avatar.designs import ANIMATION_CSS
from imbue.system_interface.avatar.designs import AvatarMood
from imbue.system_interface.avatar.designs import BUNDLED_DESIGNS
from imbue.system_interface.avatar.designs import DEFAULT_DESIGN_ID
from imbue.system_interface.avatar.designs import SVG_NAMESPACE
from imbue.system_interface.avatar.designs import bundled_design
from imbue.system_interface.avatar.designs import bundled_design_source
from imbue.system_interface.avatar.designs import parse_design_svg
from imbue.system_interface.avatar.designs import render_design_svg
from imbue.system_interface.shell.errors import InvalidShellValueError

_MINIMAL = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
    '<g class="jelly-body"><circle cx="50" cy="60" r="30" fill="#8cd"/></g>'
    '<g class="jelly-eyes"><ellipse cx="42" cy="55" rx="2" ry="3"/><ellipse cx="58" cy="55" rx="2" ry="3"/></g>'
    "</svg>"
)


def test_every_bundled_design_parses_and_is_unique() -> None:
    ids = [design.id for design in BUNDLED_DESIGNS]
    assert len(set(ids)) == len(ids)
    assert bundled_design(DEFAULT_DESIGN_ID) is not None
    for design in BUNDLED_DESIGNS:
        parse_design_svg(bundled_design_source(design))


def test_a_minimal_design_parses() -> None:
    assert parse_design_svg(_MINIMAL).get("viewBox") == "0 0 100 100"


@pytest.mark.parametrize(
    ("svg", "reason"),
    [
        ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><script>1</script></svg>', "element"),
        (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><image href="http://x/y.png"/></svg>',
            "element",
        ),
        (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><circle onclick="1" r="1"/></svg>',
            "attribute",
        ),
        ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><style>*{x:1}</style></svg>', "custom CSS"),
        ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 50 50"/>', "viewBox"),
        (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><circle r="1" fill="url(http://x)"/></svg>',
            "paint",
        ),
        ('<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"/>', "processing"),
        ('<!DOCTYPE svg [<!ENTITY x "y">]><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"/>', "invalid"),
        ("<svg viewBox='0 0 100 100'/>", "namespace"),
        ("not svg", "invalid"),
    ],
)
def test_unsafe_or_off_contract_markup_is_refused(svg: str, reason: str) -> None:
    with pytest.raises(InvalidShellValueError, match=reason):
        parse_design_svg(svg)


def test_the_shared_stylesheet_is_the_one_style_allowed() -> None:
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><style>{ANIMATION_CSS}</style></svg>'
    parse_design_svg(svg)


def _styles(rendered: str) -> list[str]:
    return [element.text or "" for element in fromstring(rendered).iter(f"{{{SVG_NAMESPACE}}}style")]


def test_a_rendered_image_wears_the_mood_and_the_current_stylesheet() -> None:
    rendered = render_design_svg(_MINIMAL, AvatarMood.WORKING, is_preview=False, design_id="my-design")
    root = fromstring(rendered)
    assert root.get("data-mood") == "working"
    assert _styles(rendered) == [ANIMATION_CSS]


def test_a_bundled_design_wakes_when_working_and_rests_when_idle() -> None:
    design = bundled_design(DEFAULT_DESIGN_ID)
    assert design is not None
    source = bundled_design_source(design)
    idle = fromstring(render_design_svg(source, AvatarMood.IDLE, is_preview=False, design_id=DEFAULT_DESIGN_ID))
    working = fromstring(render_design_svg(source, AvatarMood.WORKING, is_preview=False, design_id=DEFAULT_DESIGN_ID))
    eyes_path = f".//{{{SVG_NAMESPACE}}}g[@class='jelly-eyes']"
    assert idle.find(eyes_path).find(f"{{{SVG_NAMESPACE}}}ellipse") is None
    assert working.find(eyes_path).find(f"{{{SVG_NAMESPACE}}}ellipse") is not None
    assert len(list(working.iter(f"{{{SVG_NAMESPACE}}}style"))) == 2
    assert len(list(idle.iter(f"{{{SVG_NAMESPACE}}}style"))) == 1


def test_every_bundled_design_renders_working() -> None:
    for design in BUNDLED_DESIGNS:
        rendered = render_design_svg(bundled_design_source(design), AvatarMood.WORKING, False, design.id)
        assert 'data-mood="working"' in rendered


def test_a_preview_holds_the_pose_still() -> None:
    rendered = render_design_svg(_MINIMAL, AvatarMood.WORKING, is_preview=True, design_id="my-design")
    assert _styles(rendered)[-1] == "*{animation:none!important}"
