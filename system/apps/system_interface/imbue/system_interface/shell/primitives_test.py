import pytest

from imbue.system_interface.shell.errors import InvalidShellValueError
from imbue.system_interface.shell.primitives import ClientId
from imbue.system_interface.shell.primitives import DesktopId
from imbue.system_interface.shell.primitives import WindowId
from imbue.system_interface.shell.primitives import WindowPath
from imbue.system_interface.shell.primitives import WindowTitle
from imbue.system_interface.shell.primitives import mint_window_id


def test_desktop_ids_are_slugs() -> None:
    assert DesktopId("research-2") == "research-2"
    with pytest.raises(InvalidShellValueError):
        DesktopId("Not A Slug")


def test_window_ids_are_minted_in_the_fixed_shape() -> None:
    minted = mint_window_id()
    assert WindowId(str(minted)) == minted
    assert minted != mint_window_id()
    with pytest.raises(InvalidShellValueError):
        WindowId("tab-0123456789abcdef")


def test_client_ids_are_filename_safe() -> None:
    assert ClientId("3f2a-desktop.1") == "3f2a-desktop.1"
    with pytest.raises(InvalidShellValueError):
        ClientId("../escape")
    with pytest.raises(InvalidShellValueError):
        ClientId("")


def test_window_paths_are_rooted_on_the_apps_origin_and_titles_are_trimmed() -> None:
    assert WindowPath("/?chat=agent-1") == "/?chat=agent-1"
    for bad in ("", "notes", "//evil.example", "/\x00"):
        with pytest.raises(InvalidShellValueError):
            WindowPath(bad)
    assert WindowTitle("  Build log  ") == "Build log"
    assert WindowTitle("") == ""
    with pytest.raises(InvalidShellValueError):
        WindowTitle("x" * 300)
