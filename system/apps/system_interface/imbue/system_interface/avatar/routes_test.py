"""Tests for the avatar routes (pinned-taskbar-entries plan section 5.3)."""

import queue
from pathlib import Path

from flask import Flask
from flask.testing import FlaskClient

from imbue.system_interface.app_context import state_of
from imbue.system_interface.avatar.designs import BUNDLED_DESIGNS
from imbue.system_interface.avatar.designs import DEFAULT_DESIGN_ID
from imbue.system_interface.avatar.selection import SELECTION_FILENAME
from imbue.system_interface.shell.testing import drain_messages

_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><circle r="9" fill="#8cd"/></svg>'
_REGISTRATION = {"id": "mine", "label": "Mine", "svg": _SVG, "source_path": "/tmp/mine.svg"}
_NOT_LOOPBACK = {"REMOTE_ADDR": "10.0.0.7"}


def _window(app: Flask) -> "queue.Queue[str | None]":
    return state_of(app).shell.broadcaster.register()


def test_the_listing_offers_the_bundled_designs_and_the_default_selection(client: FlaskClient) -> None:
    listing = client.get("/api/avatars").get_json()
    assert [design["id"] for design in listing["designs"]] == [str(design.id) for design in BUNDLED_DESIGNS]
    assert listing["selected"] == listing["default"] == str(DEFAULT_DESIGN_ID)


def test_the_image_is_an_isolated_svg_wearing_the_mood(client: FlaskClient) -> None:
    response = client.get(f"/api/avatars/{DEFAULT_DESIGN_ID}/image.svg?mood=working")
    assert response.status_code == 200
    assert response.mimetype == "image/svg+xml"
    assert response.headers["Content-Security-Policy"] == "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Cache-Control"] == "no-cache"
    assert 'data-mood="working"' in response.get_data(as_text=True)
    assert 'data-mood="idle"' in client.get(f"/api/avatars/{DEFAULT_DESIGN_ID}/image.svg").get_data(as_text=True)
    assert "animation:none" in client.get(f"/api/avatars/{DEFAULT_DESIGN_ID}/image.svg?preview=1").get_data(as_text=True)
    assert client.get(f"/api/avatars/{DEFAULT_DESIGN_ID}/image.svg?mood=angry").status_code == 400
    assert client.get("/api/avatars/nobody/image.svg").status_code == 404


def test_the_source_is_the_original_as_an_attachment(client: FlaskClient) -> None:
    client.post("/api/avatars", json=_REGISTRATION)
    response = client.get("/api/avatars/mine/source.svg")
    assert response.status_code == 200
    assert response.get_data(as_text=True) == _SVG
    assert response.headers["Content-Disposition"] == 'attachment; filename="mine.svg"'
    assert client.get("/api/avatars/nobody/source.svg").status_code == 404


def test_registration_is_loopback_only_and_validated(app: Flask, client: FlaskClient) -> None:
    assert client.post("/api/avatars", json=_REGISTRATION, environ_base=_NOT_LOOPBACK).status_code == 403
    assert client.post("/api/avatars", json={**_REGISTRATION, "svg": "<svg/>"}).status_code == 400
    assert client.post("/api/avatars", json={**_REGISTRATION, "id": "Not Valid"}).status_code == 400
    response = client.post("/api/avatars", json=_REGISTRATION)
    assert response.status_code == 201
    assert response.get_json() == {"id": "mine"}
    listed = client.get("/api/avatars").get_json()["designs"][-1]
    assert listed == {"id": "mine", "label": "Mine", "source_path": "/tmp/mine.svg"}
    assert (Path(state_of(app).shell.avatar_catalog.directory) / "catalog.json").is_file()


def test_selecting_a_design_writes_the_file_and_tells_every_window(app: Flask, client: FlaskClient) -> None:
    window = _window(app)
    assert client.post("/api/avatar-selection", json={"design": "nobody"}).status_code == 400
    assert drain_messages(window) == []
    response = client.post("/api/avatar-selection", json={"design": "jelly-cat"})
    assert response.status_code == 200
    assert response.get_json() == {"design": "jelly-cat"}
    assert drain_messages(window) == [{"type": "avatar_selection_changed", "design": "jelly-cat"}]
    assert client.get("/api/avatars").get_json()["selected"] == "jelly-cat"
    assert (state_of(app).shell.avatar_selection.state_directory / SELECTION_FILENAME).is_file()
