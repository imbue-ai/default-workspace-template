"""The workspace routes that answer what the shell itself holds: the suggestion switches (which it passes to the app
that keeps them), the workspace's apps, and its wallpaper."""

from pathlib import Path
from typing import Any

import pytest
from app_schema.testing import OWNER_IDENTITY_HEADERS
from flask import Flask
from flask.testing import FlaskClient
from flask.typing import ResponseReturnValue

from imbue.system_interface.app_context import state_of
from imbue.system_interface.shell.testing import build_inventory
from imbue.system_interface.shell.testing import registry_row_toml
from imbue.system_interface.shell.testing import shell_application
from imbue.system_interface.shell.testing import suggestion_settings_app
from imbue.system_interface.shell.testing import write_two_app_registry
from imbue.system_interface.shell.wallpapers import BUNDLED_WALLPAPERS_DIRNAME
from imbue.system_interface.testing import serve_app
from imbue.system_interface.workspace_routes import APPS_ROUTE
from imbue.system_interface.workspace_routes import SHARING_ROUTE
from imbue.system_interface.workspace_routes import SUGGESTIONS_ROUTE
from imbue.system_interface.workspace_routes import WALLPAPER_ROUTE
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster

_SETTINGS_PATH = "/api/settings/suggestions"
_BOTH_ON = {"is_next_step_suggestions_enabled": True, "is_new_chat_suggestions_enabled": True}
_PNG = b"\x89PNG\r\n\x1a\n" + b"wallpaper-one"
_OTHER_PNG = b"\x89PNG\r\n\x1a\n" + b"wallpaper-two-which-is-longer"
_ICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 216 216"><rect width="216" height="216"/></svg>'


def _shell_over(
    tmp_path: Path, broadcaster: WebSocketBroadcaster, *extra_rows: str, is_preview: bool = False
) -> Flask:
    """The shell app over the two-app registry and ``extra_rows``."""
    inventory = build_inventory(write_two_app_registry(tmp_path, *extra_rows), broadcaster)
    return shell_application(tmp_path, inventory, broadcaster, is_preview=is_preview)


def _settings_holder_row(url: str) -> str:
    """The row of an app that holds the suggestion switches, named for nothing the shell knows."""
    return registry_row_toml("assistant", url, is_system=True, suggestion_settings_path=_SETTINGS_PATH)


# The suggestion switches


def test_the_suggestion_switches_are_read_from_the_app_that_holds_them(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    received: list[dict[str, Any]] = []
    holder = suggestion_settings_app(
        {
            "fast_mode_default": "auto",
            "is_next_step_suggestions_enabled": False,
            "is_new_chat_suggestions_enabled": True,
        },
        received,
        _SETTINGS_PATH,
    )
    with serve_app(holder) as served:
        client = _shell_over(tmp_path, broadcaster, _settings_holder_row(served.http_url)).test_client()

        response = client.get(SUGGESTIONS_ROUTE, headers=OWNER_IDENTITY_HEADERS)

    assert response.status_code == 200
    # Both switches and nothing else of the app's settings.
    assert response.get_json() == {"is_next_step_suggestions_enabled": False, "is_new_chat_suggestions_enabled": True}
    assert received == []


def test_a_write_names_only_the_switches_it_was_sent_and_answers_both(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    received: list[dict[str, Any]] = []
    holder = suggestion_settings_app({"fast_mode_turn_limit": 5, **_BOTH_ON}, received, _SETTINGS_PATH)
    with serve_app(holder) as served:
        client = _shell_over(tmp_path, broadcaster, _settings_holder_row(served.http_url)).test_client()

        one = client.put(SUGGESTIONS_ROUTE, json={"is_new_chat_suggestions_enabled": False})
        # A switch sent as null is left as it is, as one left out is.
        other = client.put(
            SUGGESTIONS_ROUTE,
            json={"is_next_step_suggestions_enabled": False, "is_new_chat_suggestions_enabled": None},
        )
        read = client.get(SUGGESTIONS_ROUTE)
        # A write that names no switch changes nothing, so the app is not told of a write.
        nothing = client.put(SUGGESTIONS_ROUTE, json={})

    assert one.status_code == 200
    assert (nothing.status_code, nothing.get_json()) == (200, read.get_json())
    assert one.get_json() == {"is_next_step_suggestions_enabled": True, "is_new_chat_suggestions_enabled": False}
    assert other.get_json() == {"is_next_step_suggestions_enabled": False, "is_new_chat_suggestions_enabled": False}
    assert read.get_json() == other.get_json()
    # The app is asked to change exactly what the client named, so it resets nothing else it holds.
    assert received == [{"is_new_chat_suggestions_enabled": False}, {"is_next_step_suggestions_enabled": False}]


def test_a_write_that_is_no_switch_is_refused_before_the_app_is_asked(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    received: list[dict[str, Any]] = []
    with serve_app(suggestion_settings_app(dict(_BOTH_ON), received, _SETTINGS_PATH)) as served:
        client = _shell_over(tmp_path, broadcaster, _settings_holder_row(served.http_url)).test_client()

        refused = client.put(SUGGESTIONS_ROUTE, json={"is_new_chat_suggestions_enabled": "sometimes"})

    assert refused.status_code == 400
    assert refused.get_json()["code"] == "INVALID_REQUEST"
    assert received == []


def test_a_preview_shell_reads_the_switches_and_refuses_to_change_them(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    received: list[dict[str, Any]] = []
    with serve_app(suggestion_settings_app(dict(_BOTH_ON), received, _SETTINGS_PATH)) as served:
        preview = _shell_over(tmp_path, broadcaster, _settings_holder_row(served.http_url), is_preview=True)
        client = preview.test_client()

        read = client.get(SUGGESTIONS_ROUTE)
        refused = client.put(SUGGESTIONS_ROUTE, json={"is_new_chat_suggestions_enabled": False})

    assert read.get_json() == _BOTH_ON
    assert (refused.status_code, refused.get_json()["code"]) == (403, "PREVIEW")
    assert received == []


@pytest.mark.parametrize("method", ["GET", "PUT"])
def test_the_switches_of_an_app_that_is_not_running_are_unavailable(
    tmp_path: Path, broadcaster: WebSocketBroadcaster, closed_port: int, method: str
) -> None:
    client = _shell_over(tmp_path, broadcaster, _settings_holder_row(f"http://127.0.0.1:{closed_port}")).test_client()

    response = client.open(SUGGESTIONS_ROUTE, method=method, json={})

    assert response.status_code == 503
    assert response.get_json()["code"] == "SUGGESTIONS_UNAVAILABLE"
    assert "not running" in response.get_json()["detail"]


@pytest.mark.parametrize("method", ["GET", "PUT"])
def test_a_workspace_whose_apps_declare_no_switches_does_not_support_them(client: FlaskClient, method: str) -> None:
    # The app that holds the switches registered under a template from before it named where they are.
    response = client.open(SUGGESTIONS_ROUTE, method=method, json={})

    assert response.status_code == 501
    assert response.get_json()["code"] == "SUGGESTIONS_UNSUPPORTED"


def _app_answering(path: str, body: dict[str, Any] | str, status: int) -> Flask:
    app = Flask("stand-in")

    def answer() -> ResponseReturnValue:
        return body, status

    app.add_url_rule(path, view_func=answer, methods=["GET", "PUT"], endpoint="answer")
    return app


@pytest.mark.parametrize(
    ("answering", "status", "code"),
    [
        # An app from before the path has no such route, or one that takes another method.
        (_app_answering("/api/other", {}, 200), 501, "SUGGESTIONS_UNSUPPORTED"),
        # An app from before the switches answers its settings without them.
        (
            _app_answering(_SETTINGS_PATH, {"settings": {"fast_mode_default": "off"}}, 200),
            501,
            "SUGGESTIONS_UNSUPPORTED",
        ),
        (
            _app_answering(_SETTINGS_PATH, {"settings": {**_BOTH_ON, "is_new_chat_suggestions_enabled": "yes"}}, 200),
            501,
            "SUGGESTIONS_UNSUPPORTED",
        ),
        (
            _app_answering(_SETTINGS_PATH, {"detail": "the settings file is locked"}, 500),
            503,
            "SUGGESTIONS_UNAVAILABLE",
        ),
        (_app_answering(_SETTINGS_PATH, "<html>starting</html>", 200), 503, "SUGGESTIONS_UNAVAILABLE"),
    ],
)
def test_an_app_that_answers_anything_but_its_settings_with_both_switches_is_told_apart(
    tmp_path: Path, broadcaster: WebSocketBroadcaster, answering: Flask, status: int, code: str
) -> None:
    with serve_app(answering) as served:
        client = _shell_over(tmp_path, broadcaster, _settings_holder_row(served.http_url)).test_client()

        read = client.get(SUGGESTIONS_ROUTE)
        written = client.put(SUGGESTIONS_ROUTE, json={"is_new_chat_suggestions_enabled": False})

    assert (read.status_code, read.get_json()["code"]) == (status, code)
    assert (written.status_code, written.get_json()["code"]) == (status, code)


# The apps


def test_the_apps_are_the_ones_the_sharing_read_lists_each_with_its_icon(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    client = _shell_over(
        tmp_path,
        broadcaster,
        registry_row_toml(
            "notes", "http://localhost:8410", label="notes-4f2a91c0", display_name="My notes", icon=_ICON
        ),
        registry_row_toml(
            "assistant", "http://localhost:8411", label="assistant-77aa01b2", is_system=True, icon=_ICON
        ),
        # Reached only through a share of the whole workspace, and no app a client lists.
        registry_row_toml(
            "plumbing", "http://localhost:8412", label="plumbing-0c1d2e3f", is_internal=True, icon=_ICON
        ),
    ).test_client()

    response = client.get(APPS_ROUTE, headers=OWNER_IDENTITY_HEADERS)

    assert response.status_code == 200
    apps = response.get_json()["apps"]
    assert apps == [
        {"name": "terminal", "label": "", "display_name": "Terminal", "is_system": False, "icon": ""},
        {"name": "files", "label": "", "display_name": "Files", "is_system": False, "icon": ""},
        {"name": "notes", "label": "notes-4f2a91c0", "display_name": "My notes", "is_system": False, "icon": _ICON},
        {
            "name": "assistant",
            "label": "assistant-77aa01b2",
            "display_name": "Assistant",
            "is_system": True,
            "icon": _ICON,
        },
    ]
    shareable = client.get(SHARING_ROUTE).get_json()["shareable_apps"]
    assert [{name: value for name, value in app.items() if name != "icon"} for app in apps] == shareable


# The wallpaper


def _add_wallpaper_file(app: Flask, name: str, content: bytes) -> None:
    directory = state_of(app).shell.wallpaper_files_directory
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_bytes(content)


def _wear(client: FlaskClient, desktop_id: str, wallpaper: dict[str, str] | None) -> None:
    assert client.post(f"/api/desktops/{desktop_id}/wallpaper", json={"wallpaper": wallpaper}).status_code == 200


def test_a_workspace_with_no_desktop_yet_or_one_in_its_themes_default_has_no_wallpaper_to_show(
    client: FlaskClient, app: Flask
) -> None:
    before_any_desktop = client.get(WALLPAPER_ROUTE, headers=OWNER_IDENTITY_HEADERS)

    assert (before_any_desktop.status_code, before_any_desktop.data) == (204, b"")
    # The read made no desktop: a read route changes nothing in the workspace.
    assert state_of(app).shell.desktops.list_desktops() == []

    assert [desktop["wallpaper"] for desktop in client.get("/api/desktops").get_json()["desktops"]] == [None]
    in_the_default = client.get(WALLPAPER_ROUTE)

    assert (in_the_default.status_code, in_the_default.data) == (204, b"")


def test_the_wallpaper_is_the_first_desktops_image_with_what_a_browser_needs_to_keep_it(
    client: FlaskClient, app: Flask
) -> None:
    _add_wallpaper_file(app, "mine.png", _PNG)
    _add_wallpaper_file(app, "other.png", _OTHER_PNG)
    client.get("/api/desktops")
    _wear(client, "home", {"kind": "file", "name": "mine"})

    first = client.get(WALLPAPER_ROUTE, headers=OWNER_IDENTITY_HEADERS)

    assert first.status_code == 200
    assert first.data == _PNG
    assert first.mimetype == "image/png"
    assert first.headers["Cache-Control"] == "private, no-cache"
    etag = first.headers["ETag"]

    # A browser that holds the image asks again with what it holds, and is sent nothing while it has not changed.
    unchanged = client.get(WALLPAPER_ROUTE, headers={"If-None-Match": etag})

    assert (unchanged.status_code, unchanged.data) == (304, b"")
    assert unchanged.headers["Cache-Control"] == "private, no-cache"

    _wear(client, "home", {"kind": "file", "name": "other"})
    changed = client.get(WALLPAPER_ROUTE, headers={"If-None-Match": etag})

    assert (changed.status_code, changed.data) == (200, _OTHER_PNG)
    assert changed.headers["ETag"] != etag


def test_a_bundled_wallpaper_is_answered_under_its_own_type(client: FlaskClient, app: Flask) -> None:
    bundled = state_of(app).static_directory / BUNDLED_WALLPAPERS_DIRNAME
    bundled.mkdir(parents=True)
    (bundled / "dunes.jpg").write_bytes(b"\xff\xd8\xff-dunes")
    client.get("/api/desktops")
    _wear(client, "home", {"kind": "bundled", "name": "dunes"})

    response = client.get(WALLPAPER_ROUTE)

    assert (response.status_code, response.mimetype, response.data) == (200, "image/jpeg", b"\xff\xd8\xff-dunes")


def test_a_later_desktops_wallpaper_is_not_the_workspaces(client: FlaskClient, app: Flask) -> None:
    _add_wallpaper_file(app, "mine.png", _PNG)
    client.get("/api/desktops")
    assert client.post("/api/desktops", json={"name": "Research"}).status_code == 201
    _wear(client, "research", {"kind": "file", "name": "mine"})

    assert client.get(WALLPAPER_ROUTE).status_code == 204

    # Once it is moved to the front it is the first, and its wallpaper the one a client paints with.
    assert client.post("/api/desktops/order", json={"desktop_ids": ["research", "home"]}).status_code == 200

    assert client.get(WALLPAPER_ROUTE).data == _PNG


def test_a_wallpaper_whose_image_is_gone_is_none_to_show(client: FlaskClient, app: Flask) -> None:
    _add_wallpaper_file(app, "mine.png", _PNG)
    client.get("/api/desktops")
    _wear(client, "home", {"kind": "file", "name": "mine"})
    (state_of(app).shell.wallpaper_files_directory / "mine.png").unlink()

    response = client.get(WALLPAPER_ROUTE)

    assert (response.status_code, response.data) == (204, b"")
