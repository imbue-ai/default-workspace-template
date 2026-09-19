from pathlib import Path

from imbue.system_interface.shell.data_types import Wallpaper
from imbue.system_interface.shell.primitives import WallpaperKind
from imbue.system_interface.shell.primitives import WallpaperName
from imbue.system_interface.shell.wallpapers import WallpaperDirectories
from imbue.system_interface.shell.wallpapers import list_wallpapers
from imbue.system_interface.shell.wallpapers import resolve_wallpaper_file
from imbue.system_interface.shell.wallpapers import wallpaper_listing_wire_json


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

    listed = [wallpaper_listing_wire_json(listing) for listing in list_wallpapers(directories)]
    assert listed == [
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


def test_missing_directories_list_nothing(tmp_path: Path) -> None:
    directories = WallpaperDirectories(bundled=tmp_path / "nowhere", files=tmp_path / "nowhere-else")
    assert list_wallpapers(directories) == []
