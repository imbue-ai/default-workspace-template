from pathlib import Path
from typing import Final

from PIL import Image

from workspace_themes.contract import (
    BUILTIN_THEMES_DIRECTORY,
    STANDARD_THEME_ID,
    THEME_MANIFEST_FILENAME,
)

TEST_ICON_SIZE: Final[int] = 32

_STANDARD_MANIFEST: Final[str] = """contract = 1
id = "standard"
name = "Standard"
description = "The workspace's own look."

[icons]
guide = "icons/guide.md"
format = "svg"
size = 216
rendering = "smooth"
background = "transparent"
derive = "none"
"""


def write_test_icon(path: Path, size: int) -> None:
    """A legible pixel icon: a black square with a white center on a clear canvas."""
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    for y in range(size // 4, size - size // 4):
        for x in range(size // 4, size - size // 4):
            is_center = (
                size // 3 <= x < size - size // 3 and size // 3 <= y < size - size // 3
            )
            image.putpixel(
                (x, y), (255, 255, 255, 255) if is_center else (0, 0, 0, 255)
            )
    image.save(path, format="PNG")


def write_standard_theme(repo_root: Path) -> Path:
    folder = repo_root / BUILTIN_THEMES_DIRECTORY / STANDARD_THEME_ID
    (folder / "icons").mkdir(parents=True, exist_ok=True)
    (folder / THEME_MANIFEST_FILENAME).write_text(_STANDARD_MANIFEST, encoding="utf-8")
    (folder / "icons" / "guide.md").write_text("# Standard icons\n", encoding="utf-8")
    return folder


def write_test_theme(
    root: Path,
    theme_id: str,
    parts_css: str,
    base: str | None = None,
    app_names_with_icons: tuple[str, ...] = (),
) -> Path:
    """A small valid pixel theme under ``root`` (a theme root such as system/themes or themes) with one style file."""
    folder = root / theme_id
    (folder / "icons").mkdir(parents=True, exist_ok=True)
    base_line = f'base = "{base}"\n' if base is not None else ""
    (folder / THEME_MANIFEST_FILENAME).write_text(
        f'contract = 1\nid = "{theme_id}"\nname = "Test {theme_id}"\ndescription = "A theme for tests."\n'
        f'{base_line}\n[styles]\nfiles = ["parts.css"]\n\n[chrome]\ntitle_align = "center"\n'
        f'leading = ["close", "title", "refresh", "menu"]\ntrailing = ["minimize", "maximize"]\n\n'
        f'[icons]\nguide = "icons/guide.md"\nformat = "png"\nsize = {TEST_ICON_SIZE}\nrendering = "pixelated"\n'
        f'background = "transparent"\npalette = ["#000000", "#ffffff"]\nderive = "monochrome"\n',
        encoding="utf-8",
    )
    (folder / "parts.css").write_text(parts_css, encoding="utf-8")
    (folder / "icons" / "guide.md").write_text(
        f"# {theme_id} icons\n", encoding="utf-8"
    )
    write_test_icon(folder / "icons" / "app.png", TEST_ICON_SIZE)
    for app_name in app_names_with_icons:
        write_test_icon(folder / "icons" / f"{app_name}.png", TEST_ICON_SIZE)
    return folder
