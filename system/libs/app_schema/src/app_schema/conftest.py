from pathlib import Path

import pytest

_NOTES_MANIFEST = """
name = "notes"
display_name = "Notes"
icon = "icon.svg"
description = "Keeps the person's notes and finds them again."
api_schema_path = "/api/schema"
"""


@pytest.fixture
def notes_manifest_path(tmp_path: Path) -> Path:
    """The manifest of a ``notes`` app that describes its API at ``/api/schema``, with the icon it names beside it."""
    manifest = tmp_path / "app.toml"
    manifest.write_text(_NOTES_MANIFEST)
    (tmp_path / "icon.svg").write_text("<svg/>")
    return manifest
