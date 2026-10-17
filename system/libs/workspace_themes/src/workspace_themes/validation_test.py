from pathlib import Path

import pytest

from workspace_themes.contract import (
    CONTRACT_VERSION,
    CORE_PARTS,
    MAX_STYLE_FILES_BYTES,
    MAX_THEME_FOLDER_BYTES,
)
from workspace_themes.data_types import ResolvedChrome, ThemeIconSpec, ThemeManifest
from workspace_themes.primitives import (
    ChromeSlot,
    IconBackground,
    IconDerivation,
    IconFormat,
    IconRendering,
    ThemeRelativePath,
    TitleAlign,
)
from workspace_themes.testing import write_png_header_only
from workspace_themes.validation import (
    StyleSheetContext,
    check_attribution_files,
    check_folder_files,
    check_icon_files,
    check_manifest_rules,
    check_resolved_chrome,
    check_style_files,
    check_style_sheet,
    read_theme_manifest,
)


def _context(
    folder: Path, allowed_parts: frozenset[str] = frozenset(CORE_PARTS)
) -> StyleSheetContext:
    return StyleSheetContext(
        label="parts.css", folder=folder, file_directory="", allowed_parts=allowed_parts
    )


def _manifest(**fields: object) -> ThemeManifest:
    return ThemeManifest.model_validate(
        {
            "contract": CONTRACT_VERSION,
            "id": "paper",
            "name": "Paper",
            "description": "A paper theme.",
            **fields,
        }
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
            '[data-part="window"] { background: url("%2e%2e/%2E%2e/api/x.png?q=1"); }',
            "leaves the theme folder",
        ),
        (
            '[data-part="window"] { background: url(.%2e/a.png); }',
            "leaves the theme folder",
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
            '[data-part="window"] { background: image-set(if(media(width > 0): "https://example.com/a.png") 1x); }',
            "image-set() takes if()",
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
        (
            "@keyframes blink { @media print { from { opacity: 0; } } }",
            "@media is not allowed inside @keyframes",
        ),
    ],
)
def test_css_that_steps_outside_the_contract_is_named(
    tmp_path: Path, css: str, expected: str
) -> None:
    problems = check_style_sheet(css, _context(tmp_path))

    assert any(expected in problem for problem in problems), problems


def test_a_url_may_name_only_a_kind_of_file_a_theme_serves(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text("# Notes\n", encoding="utf-8")

    problems = check_style_sheet(
        '[data-part="window"] { background: url("notes.md"); }', _context(tmp_path)
    )

    assert problems == [
        "parts.css:1: url(notes.md) names notes.md, a kind of file a theme does not serve"
    ]


def test_an_app_overlay_may_style_the_parts_its_app_declares(tmp_path: Path) -> None:
    allowed = frozenset(CORE_PARTS) | {"chat.user-message"}

    problems = check_style_sheet(
        '[data-part="chat.user-message"] { color: #000000; }',
        _context(tmp_path, allowed),
    )

    assert problems == []


def test_an_app_overlay_defines_no_animation_or_font(tmp_path: Path) -> None:
    context = StyleSheetContext(
        label="apps/chat.css",
        folder=tmp_path,
        file_directory="apps",
        allowed_parts=frozenset(CORE_PARTS) | {"chat.user-message"},
        scoping_part_prefix="chat.",
    )

    problems = check_style_sheet(
        "@keyframes theme-pulse { from { opacity: 0; } }\n"
        '@font-face { font-family: "Chicago"; }\n'
        '@media (min-width: 1px) { [data-part="chat.user-message"] { color: #000000; } }\n',
        context,
    )

    assert problems == [
        "apps/chat.css:1: @keyframes names something every app's page shares; put it in the theme's own style "
        "files, not an app's overlay",
        "apps/chat.css:2: @font-face names something every app's page shares; put it in the theme's own style "
        "files, not an app's overlay",
    ]


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


_ICONS = {
    "guide": "icons/guide.md",
    "format": "png",
    "size": 32,
    "rendering": "pixelated",
    "background": "transparent",
    "derive": "none",
}


@pytest.mark.parametrize(
    ("folder_name", "fields", "expected"),
    [
        ("paper", {}, []),
        (
            "ink",
            {},
            ["theme.toml: id 'paper' must equal the folder's name 'ink'"],
        ),
        (
            "paper",
            {"contract": CONTRACT_VERSION + 1},
            [
                f"theme.toml: contract {CONTRACT_VERSION + 1} is not one this workspace reads (it reads "
                f"{CONTRACT_VERSION})"
            ],
        ),
        (
            "paper",
            {"base": "paper"},
            ["theme.toml: a theme cannot be its own base"],
        ),
        ("standard", {"id": "standard", "icons": _ICONS}, []),
        (
            "standard",
            {"id": "standard", "base": "paper", "icons": _ICONS},
            ["theme.toml: the standard theme has no base"],
        ),
        (
            "standard",
            {"id": "standard"},
            ["theme.toml: the standard theme must declare [icons]"],
        ),
    ],
)
def test_a_manifest_breaks_each_rule_the_catalog_holds_it_to(
    folder_name: str, fields: dict[str, object], expected: list[str]
) -> None:
    assert check_manifest_rules(_manifest(**fields), folder_name) == expected


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


def test_a_manifest_that_is_not_utf_8_is_a_problem_not_a_crash(tmp_path: Path) -> None:
    (tmp_path / "theme.toml").write_bytes(b'name = "Caf\xe9"\n')

    manifest, problems = read_theme_manifest(tmp_path)

    assert manifest is None
    assert len(problems) == 1
    assert problems[0].startswith("theme.toml is not UTF-8 text")


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


def test_hidden_files_a_theme_never_serves_are_left_alone(tmp_path: Path) -> None:
    (tmp_path / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")
    (tmp_path / ".parts.css.swp").write_bytes(b"b0VIM")
    (tmp_path / "icons").mkdir()
    (tmp_path / "icons" / ".gitkeep").write_text("", encoding="utf-8")
    (tmp_path / ".git" / "objects" / "ab").mkdir(parents=True)
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (tmp_path / ".git" / "objects" / "ab" / "cdef").write_bytes(b"x" * 1024)
    (tmp_path / "evil.js").write_text("alert(1)", encoding="utf-8")

    problems = check_folder_files(tmp_path)

    assert problems == ["evil.js: a theme may not contain .js files"]


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


def test_a_theme_s_png_icon_too_large_to_open_is_a_problem_not_a_crash(
    tmp_path: Path,
) -> None:
    (tmp_path / "icons").mkdir()
    (tmp_path / "icons" / "guide.md").write_text("Pixel glyphs.\n")
    write_png_header_only(tmp_path / "icons" / "app.png", 20000)
    spec = ThemeIconSpec(
        guide=ThemeRelativePath("icons/guide.md"),
        format=IconFormat.PNG,
        size=32,
        rendering=IconRendering.PIXELATED,
        background=IconBackground.TRANSPARENT,
        derive=IconDerivation.NONE,
    )

    problems = check_icon_files(tmp_path, "paper", spec)

    assert len(problems) == 1
    assert problems[0].startswith("icons/app.png: cannot be read as an image")


def test_each_icon_file_the_spec_names_that_is_missing_is_named(
    tmp_path: Path,
) -> None:
    spec = ThemeIconSpec(
        guide=ThemeRelativePath("icons/guide.md"),
        references=(ThemeRelativePath("icons/chat.png"),),
        format=IconFormat.PNG,
        size=32,
        rendering=IconRendering.PIXELATED,
        background=IconBackground.TRANSPARENT,
        derive=IconDerivation.NONE,
    )

    problems = check_icon_files(tmp_path, "paper", spec)

    assert problems == [
        "icons.guide: icons/guide.md does not exist",
        "icons.references: icons/chat.png does not exist",
        "icons: the generic program icon icons/app.png is missing",
    ]


def test_each_path_an_attribution_covers_that_is_missing_is_named(
    tmp_path: Path,
) -> None:
    (tmp_path / "fonts").mkdir()
    manifest = _manifest(
        attribution=[
            {
                "name": "Lib",
                "url": "https://example.com/lib",
                "license": "MIT",
                "files": ["fonts", "vendor/LICENSE-lib"],
            }
        ]
    )

    problems = check_attribution_files(tmp_path, manifest)

    assert problems == ["attribution 'Lib': vendor/LICENSE-lib does not exist"]


def test_a_folder_over_its_size_limit_is_named(tmp_path: Path) -> None:
    # Sparse, so the test writes no 16 MB of data.
    with (tmp_path / "backdrop.png").open("wb") as backdrop:
        backdrop.truncate(MAX_THEME_FOLDER_BYTES + 1)

    problems = check_folder_files(tmp_path)

    assert problems == [
        f"the folder is {MAX_THEME_FOLDER_BYTES + 1} bytes, more than the {MAX_THEME_FOLDER_BYTES} a theme may be"
    ]


def test_style_files_over_their_size_limit_are_named(tmp_path: Path) -> None:
    css = "/*" + "x" * MAX_STYLE_FILES_BYTES + "*/"
    (tmp_path / "parts.css").write_text(css, encoding="utf-8")

    problems = check_style_files(
        tmp_path, _manifest(styles={"files": ["parts.css"]}), {}
    )

    assert problems == [
        f"the style files are {len(css)} bytes, more than the {MAX_STYLE_FILES_BYTES} allowed"
    ]
