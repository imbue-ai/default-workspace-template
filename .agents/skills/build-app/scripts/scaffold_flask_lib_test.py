from pathlib import Path

import pytest
import scaffold_flask_lib
from app_manifest.manifest import load_manifest
from app_manifest.primitives import MAX_DISPLAY_NAME_LENGTH

_ICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M2 2h20v20H2z"/></svg>'


def test_write_lib_writes_a_manifest_the_library_accepts(tmp_path: Path) -> None:
    lib_dir = scaffold_flask_lib._write_lib(
        tmp_path, "inbox-status", "inbox status dashboard", "Inbox status", 8081, [], _ICON
    )

    manifest = load_manifest(lib_dir / "app.toml")

    assert lib_dir == tmp_path / "system" / "apps" / "inbox_status"
    assert manifest.name == "inbox-status"
    assert manifest.display_name == "Inbox status"
    assert manifest.icon == "icon.svg"
    assert manifest.instances is False
    assert manifest.priority == "user"
    assert manifest.program == "inbox-status"
    assert manifest.default_shortcut is None


def test_display_name_falls_back_to_the_description() -> None:
    assert scaffold_flask_lib._display_name("inbox status dashboard", None) == "inbox status dashboard"
    assert scaffold_flask_lib._display_name("inbox status dashboard", " Inbox ") == "Inbox"


@pytest.mark.parametrize("candidate", ["", "   ", "x" * 65, 'say "hi"'])
def test_display_name_refuses_what_the_manifest_would_not_take(candidate: str) -> None:
    with pytest.raises(SystemExit):
        scaffold_flask_lib._display_name("description", candidate)


def test_the_display_name_limit_matches_the_library() -> None:
    # The scaffold runs in its own PEP 723 environment and cannot import the
    # library, so it carries its own copy of the limit.
    assert scaffold_flask_lib.MAX_DISPLAY_NAME_LENGTH == MAX_DISPLAY_NAME_LENGTH
