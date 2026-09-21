"""Tests for the registration helper an agent runs against the running shell."""

from pathlib import Path

import pytest
from flask import Flask

from imbue.system_interface.app_context import state_of
from imbue.system_interface.avatar.designs import DEFAULT_DESIGN_ID
from imbue.system_interface.avatar.designs import MAX_SVG_BYTES
from imbue.system_interface.avatar.register_avatar import AvatarRegistrationError
from imbue.system_interface.avatar.register_avatar import main
from imbue.system_interface.testing import serve_app

_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><circle r="9" fill="#8cd"/></svg>'


def _run(shell_url: str, source: Path, design_id: str, *extra: str) -> None:
    main(["--source", str(source), "--id", design_id, "--label", "Mine", "--shell-url", shell_url, *extra])


def test_the_helper_registers_a_file_and_selects_it_when_asked(app: Flask, tmp_path: Path) -> None:
    source = tmp_path / "mine.svg"
    source.write_text(_SVG)
    shell = state_of(app).shell
    with serve_app(app) as served:
        _run(served.http_url, source, "mine")
        assert shell.avatar_catalog.source("mine") == _SVG
        assert shell.avatar_selection.read() == DEFAULT_DESIGN_ID
        _run(served.http_url, source, "mine", "--select")
    assert shell.avatar_selection.read() == "mine"
    (registered,) = [listing for listing in shell.avatar_catalog.entries() if listing.id == "mine"]
    assert registered.source_path == str(source.resolve())


def test_the_helper_names_the_reason_a_design_is_refused(app: Flask, tmp_path: Path) -> None:
    fine = tmp_path / "fine.svg"
    fine.write_text(_SVG)
    custom = tmp_path / "custom.svg"
    custom.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><style>*{x:1}</style></svg>')
    oversized = tmp_path / "big.svg"
    oversized.write_bytes(b" " * (MAX_SVG_BYTES + 1))
    binary = tmp_path / "binary.svg"
    binary.write_bytes(b"\xff\xfe")
    with serve_app(app) as served:
        # The shell's own refusal reaches the caller with its reason.
        with pytest.raises(AvatarRegistrationError, match="bundled"):
            _run(served.http_url, fine, str(DEFAULT_DESIGN_ID))
        with pytest.raises(AvatarRegistrationError, match="custom CSS"):
            _run(served.http_url, custom, "custom")
        with pytest.raises(AvatarRegistrationError, match="exceeds"):
            _run(served.http_url, oversized, "big")
        with pytest.raises(AvatarRegistrationError, match="UTF-8"):
            _run(served.http_url, binary, "binary")
    assert state_of(app).shell.avatar_catalog.entries()[-1].source_path is None
