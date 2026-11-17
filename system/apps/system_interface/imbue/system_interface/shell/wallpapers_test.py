from pathlib import Path

from workspace_layout.primitives import WallpaperKind
from workspace_layout.primitives import WallpaperName
from workspace_layout.records import Wallpaper
from workspace_themes.workspace_color import WorkspaceColorRecord

from imbue.system_interface.shell.wallpapers import WallpaperDirectories
from imbue.system_interface.shell.wallpapers import WallpapersListing
from imbue.system_interface.shell.wallpapers import is_studio_wallpaper
from imbue.system_interface.shell.wallpapers import list_wallpapers
from imbue.system_interface.shell.wallpapers import resolve_wallpaper_file
from imbue.system_interface.shell.wallpapers import studio_wallpaper_svg


def test_wallpapers_are_listed_bundled_first_with_accepted_images_only(tmp_path: Path) -> None:
    bundled = tmp_path / "bundled"
    files = tmp_path / "files"
    bundled.mkdir()
    files.mkdir()
    (bundled / "dunes.jpg").write_bytes(b"jpg")
    (bundled / "apricot-coast.png").write_bytes(b"png")
    (bundled / "notes.txt").write_text("not an image")
    (files / "mine.webp").write_bytes(b"webp")
    (files / "bad name!.png").write_bytes(b"png")
    directories = WallpaperDirectories(bundled=bundled, files=files)

    listed = WallpapersListing(wallpapers=tuple(list_wallpapers(directories))).model_dump(mode="json")["wallpapers"]
    assert listed == [
        {"kind": "bundled", "name": "studio", "url": "/wallpapers/bundled/studio"},
        {"kind": "bundled", "name": "apricot-coast", "url": "/wallpapers/bundled/apricot-coast"},
        {"kind": "bundled", "name": "dunes", "url": "/wallpapers/bundled/dunes"},
        {"kind": "file", "name": "mine", "url": "/wallpapers/file/mine"},
    ]
    assert resolve_wallpaper_file(Wallpaper(kind=WallpaperKind.FILE, name=WallpaperName("mine")), directories) == (
        files / "mine.webp"
    )
    assert resolve_wallpaper_file(Wallpaper(kind=WallpaperKind.FILE, name=WallpaperName("dunes")), directories) is None
    assert (
        resolve_wallpaper_file(Wallpaper(kind=WallpaperKind.BUNDLED, name=WallpaperName("notes")), directories) is None
    )


def test_missing_directories_list_only_the_drawn_studio_wallpaper(tmp_path: Path) -> None:
    directories = WallpaperDirectories(bundled=tmp_path / "nowhere", files=tmp_path / "nowhere-else")
    assert [listing.name for listing in list_wallpapers(directories)] == ["studio"]


def test_the_studio_wallpaper_is_drawn_in_the_workspace_color_s_wallpaper_ramp() -> None:
    imbue = studio_wallpaper_svg(WorkspaceColorRecord(hex=None))
    teal = studio_wallpaper_svg(WorkspaceColorRecord(hex="#0b292b"))

    assert imbue == studio_wallpaper_svg(WorkspaceColorRecord(hex="#492222"))
    assert 'fill="#fdeedb"' in imbue
    assert 'fill="#e9f2f3"' in teal
    assert 'preserveAspectRatio="xMidYMax meet"' in teal
    assert is_studio_wallpaper(Wallpaper(kind=WallpaperKind.BUNDLED, name=WallpaperName("studio")))
    assert not is_studio_wallpaper(Wallpaper(kind=WallpaperKind.FILE, name=WallpaperName("studio")))
