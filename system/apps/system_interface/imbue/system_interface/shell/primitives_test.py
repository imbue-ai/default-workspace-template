import pytest
from workspace_layout.primitives import WindowId

from imbue.system_interface.shell.errors import InvalidShellValueError
from imbue.system_interface.shell.primitives import WindowPath
from imbue.system_interface.shell.primitives import WindowTitle
from imbue.system_interface.shell.primitives import mint_window_id


def test_minted_window_ids_are_distinct_and_in_the_fixed_shape() -> None:
    minted = mint_window_id()
    assert WindowId(str(minted)) == minted
    assert minted != mint_window_id()


def test_window_paths_are_rooted_on_the_apps_origin_and_titles_are_trimmed() -> None:
    assert WindowPath("/?chat=agent-1") == "/?chat=agent-1"
    for bad in ("", "notes", "//evil.example", "/\x00"):
        with pytest.raises(InvalidShellValueError):
            WindowPath(bad)
    assert WindowTitle("  Build log  ") == "Build log"
    assert WindowTitle("") == ""
    with pytest.raises(InvalidShellValueError):
        WindowTitle("x" * 300)
