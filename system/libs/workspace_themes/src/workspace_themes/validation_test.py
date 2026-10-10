from pathlib import Path

import pytest

from workspace_themes.contract import CORE_PARTS
from workspace_themes.data_types import ResolvedChrome, ThemeIconSpec
from workspace_themes.primitives import (
    ChromeSlot,
    IconBackground,
    IconDerivation,
    IconFormat,
    IconRendering,
    ThemeRelativePath,
    TitleAlign,
)
from workspace_themes.validation import (
    StyleSheetContext,
    check_folder_files,
    check_icon_files,
    check_resolved_chrome,
    check_style_sheet,
    read_theme_manifest,
)


def _context(
    folder: Path, allowed_parts: frozenset[str] = frozenset(CORE_PARTS)
) -> StyleSheetContext:
    return StyleSheetContext(
        label="parts.css", folder=folder, file_directory="", allowed_parts=allowed_parts
    )


def test_contract_css_passes(tmp_path: Path) -> None:
    (tmp_path / "fonts").mkdir()
    (tmp_path / "fonts" / "face.woff2").write_bytes(b"woff")
    css = """
    @font-face { font-family: "Face"; src: url("fonts/face.woff2") format("woff2"); }
    :root { --c-accent: #000000; --theme-bevel: inset 1px 1px #fff; --desk-title-bar-height: 20px; }
    :root[data-touch] { --desk-title-bar-height: 44px; }
    [data-part="window"][data-focused="true"] [data-part="title-bar"] { background: url("data:image/svg+xml,x"); }
    [data-part="window-control"]:is([data-control="close"], [data-control="menu"]):active > * { display: none; }
    input[type="text"], ::selection, a:hover { color: #000000; }
    [data-part="list-row"]:nth-child(2n+1) { background: var(--c-surface-secondary); }
    @media (prefers-reduced-motion: no-preference) { [data-part="button"] { transition: none; } }
    @keyframes blink { from { opacity: 0; } to { opacity: 1; } }
    """

    assert check_style_sheet(css, _context(tmp_path)) == []


@pytest.mark.parametrize(
    ("css", "expected"),
    [
        (".title-bar { color: red; }", ".title-bar is a class"),
        ("[data-part='window'] .x { color: red; }", ".x is a class"),
        (":not(.x) { color: red; }", ".x is a class"),
        ("#root { color: red; }", "#root is an id selector"),
        ("[class~=btn] { color: red; }", "[class] selects an app's private markup"),
        (
            '[data-part="chat.user-message"] { color: red; }',
            "'chat.user-message' is not a part this file may style",
        ),
        ('[data-part^="win"] { color: red; }', "select a part exactly"),
        (
            ":root { --si-pane-radius: 4px; }",
            "--si-pane-radius is not a contract token",
        ),
        (":root { --desk-title-bar-height: 9px; }", "is 9px, outside 16px to 64px"),
        (":root { --desk-title-bar-height: 2em; }", "must be a length in px"),
        ('@import url("other.css");', "@import is not allowed"),
        ("@layer theme { :root { color: red; } }", "@layer is not allowed"),
        (
            '[data-part="window"] { background: url("https://example.com/a.png"); }',
            "is remote",
        ),
        (
            '[data-part="window"] { background: url("../other/a.png"); }',
            "leaves the theme folder",
        ),
        (
            '[data-part="window"] { background: url("missing.png"); }',
            "names missing.png, which does not exist",
        ),
        (
            '[data-part="window"] { background: image-set("https://example.com/a.png" 1x); }',
            "is remote",
        ),
        (
            '[data-part="window"] { background: -webkit-image-set("missing.png" 1x); }',
            "names missing.png, which does not exist",
        ),
        (
            '[data-part="window"] { [data-part="title-bar"] { color: red; } }',
            "nested rules are not supported",
        ),
        (
            '[data-part="window"] { background: image-set(var(--theme-image) 1x); }',
            "image-set() takes var()",
        ),
        (
            '[data-part="window"] { background: -webkit-image-set(attr(data-x) 1x); }',
            "-webkit-image-set() takes attr()",
        ),
        (
            r'[data-part="window"] { background: url("\\evil.com/a.png"); }',
            "has a backslash",
        ),
        (
            '[data-part="window"] { background: url("/\t/evil.com/a.png"); }',
            "url(//evil.com/a.png) is remote",
        ),
        ('[*|class~="foo"] { color: red; }', "[class] selects an app's private markup"),
        ("[|id] { color: red; }", "[id] selects an app's private markup"),
        ('[*|data-part^="chat"] { color: red; }', "select a part exactly"),
        (":nth-child(1 of .private) { color: red; }", ".private is a class"),
        ("::slotted(.x) { color: red; }", ".x is a class"),
    ],
)
def test_css_that_steps_outside_the_contract_is_named(
    tmp_path: Path, css: str, expected: str
) -> None:
    problems = check_style_sheet(css, _context(tmp_path))

    assert any(expected in problem for problem in problems), problems


def test_an_app_overlay_may_style_the_parts_its_app_declares(tmp_path: Path) -> None:
    allowed = frozenset(CORE_PARTS) | {"chat.user-message"}

    problems = check_style_sheet(
        '[data-part="chat.user-message"] { color: #000000; }',
        _context(tmp_path, allowed),
    )

    assert problems == []


def test_chrome_keeps_every_control_once() -> None:
    chrome = ResolvedChrome(
        title_align=TitleAlign.CENTER,
        leading=(ChromeSlot.CLOSE, ChromeSlot.TITLE, ChromeSlot.CLOSE),
        trailing=(ChromeSlot.MAXIMIZE,),
    )

    problems = check_resolved_chrome(chrome)

    assert problems == [
        "chrome: close appear more than once across leading and trailing",
        "chrome: every control stays on the title bar, but menu, minimize, refresh is missing",
    ]


def test_a_manifest_with_an_unknown_key_or_a_bad_value_names_each(
    tmp_path: Path,
) -> None:
    (tmp_path / "theme.toml").write_text(
        'contract = 1\nid = "x"\nname = ""\ndescription = "d"\ncolour = "red"\n',
        encoding="utf-8",
    )

    manifest, problems = read_theme_manifest(tmp_path)

    assert manifest is None
    assert any(problem.startswith("theme.toml: name:") for problem in problems)
    assert any(problem.startswith("theme.toml: colour:") for problem in problems)


def test_a_style_file_may_not_take_the_bundle_s_name_at_the_top_of_the_folder(
    tmp_path: Path,
) -> None:
    (tmp_path / "theme.css").write_text("", encoding="utf-8")
    (tmp_path / "apps").mkdir()
    (tmp_path / "apps" / "theme.css").write_text("", encoding="utf-8")

    problems = check_folder_files(tmp_path)

    assert problems == [
        "theme.css: theme.css is the bundle's name; call the style file something else"
    ]


def test_a_theme_folder_may_not_hold_scripts_or_links(tmp_path: Path) -> None:
    (tmp_path / "theme.toml").write_text("", encoding="utf-8")
    (tmp_path / "LICENSE-lib").write_text("MIT", encoding="utf-8")
    (tmp_path / "evil.js").write_text("alert(1)", encoding="utf-8")
    (tmp_path / "link.css").symlink_to(tmp_path / "theme.toml")

    problems = check_folder_files(tmp_path)

    assert problems == [
        "evil.js: a theme may not contain .js files",
        "link.css: a theme folder may not contain links",
    ]


def test_a_theme_s_own_svg_icons_are_held_to_the_svg_icon_rules(tmp_path: Path) -> None:
    icons_directory = tmp_path / "icons"
    icons_directory.mkdir()
    (icons_directory / "guide.md").write_text("Flat glyphs.\n")
    (icons_directory / "app.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><rect width="24" height="24"/></svg>'
    )
    (icons_directory / "chat.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><script>alert(1)</script></svg>'
    )
    spec = ThemeIconSpec(
        guide=ThemeRelativePath("icons/guide.md"),
        format=IconFormat.SVG,
        size=216,
        rendering=IconRendering.SMOOTH,
        background=IconBackground.TRANSPARENT,
        derive=IconDerivation.NONE,
    )

    problems = check_icon_files(tmp_path, "paper", spec)

    assert problems == [
        "icons/chat.svg: the icon has a <script> element, which an icon may not"
    ]
