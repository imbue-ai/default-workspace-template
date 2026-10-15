import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from studio_discord import server, studio


class _FakeShell(BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_GET(self) -> None:  # noqa: N802 -- the stdlib's name
        _FakeShell.hits.append(self.path)
        if self.path == "/_static/app_contract.js":
            body = b"export const connectToShell = () => ({});"
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 -- the stdlib's name
        pass


@pytest.fixture
def fake_shell(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FakeShell)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    monkeypatch.setattr(studio, "SHELL_URL", url)
    studio.clear_module_cache()
    _FakeShell.hits = []
    yield url
    httpd.shutdown()
    studio.clear_module_cache()


@pytest.fixture
def client():  # noqa: ANN201 -- flask's test client type is private
    return server.app.test_client()


def test_index_serves_the_page_with_the_shell_script(client) -> None:
    response = client.get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Talk to the Studio team" in html
    assert 'from "/_static/app_contract.js"' in html
    assert "__SHELL_PAGE_SCRIPT__" not in html


def test_shell_module_is_fetched_from_the_shell_once_then_cached(client, fake_shell: str) -> None:
    first = client.get("/_static/app_contract.js")
    second = client.get("/_static/app_contract.js")
    assert first.status_code == 200
    assert first.mimetype == "text/javascript"
    assert b"connectToShell" in first.data and second.data == first.data
    assert _FakeShell.hits == ["/_static/app_contract.js"]


def test_unknown_module_is_404_without_asking_the_shell(client, fake_shell: str) -> None:
    assert client.get("/_static/../../etc/passwd").status_code == 404
    assert client.get("/_static/other.js").status_code == 404
    assert _FakeShell.hits == []


def test_shell_missing_the_module_is_a_502(client, fake_shell: str) -> None:
    assert client.get("/_static/context_menu.js").status_code == 502


def test_shell_unreachable_is_a_502(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(studio, "SHELL_URL", "http://127.0.0.1:9")
    studio.clear_module_cache()
    assert client.get("/_static/app_contract.js").status_code == 502


def test_health(client) -> None:
    assert client.get("/health").get_json() == {"status": "ok"}


# --- the real routes, with Studio and Discord faked ---

from pathlib import Path  # noqa: E402

from studio_discord import config, discord  # noqa: E402
from studio_discord.store import Store  # noqa: E402
from studio_discord.web import HttpError, UnreachableError  # noqa: E402


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, object]:
    """A fake Studio and Discord; tests flip its fields."""
    w: dict[str, object] = {
        "granted": False,
        "requests": [],
        "removed": [],
        "restored": None,
        "reads": [],
        "fail": None,
    }
    monkeypatch.setattr(server, "store", Store(tmp_path))
    monkeypatch.setattr(server.discord, "widget", lambda: {"name": "Imbue", "presence_count": 9, "members": []})
    monkeypatch.setattr(server.invite, "join_link", lambda widget: "https://discord.gg/test")
    monkeypatch.setattr(server.studio, "discord_access_granted", lambda: w["granted"])
    monkeypatch.setattr(server.studio, "request_discord_access", lambda: w["requests"].append(1) or "req-1")  # type: ignore[union-attr]
    monkeypatch.setattr(server.studio, "remove_desktop_icons", lambda: [{"desktop_id": "home", "shortcut": {}}])
    monkeypatch.setattr(server.studio, "restore_desktop_icons", lambda removed: w.__setitem__("restored", removed))
    monkeypatch.setattr(server.discord, "me", lambda: {"id": "1", "username": "faraway9911", "avatar": None})

    def read(path_kind: str, channel_id: str) -> object:
        w["reads"].append((path_kind, channel_id))  # type: ignore[union-attr]
        if w["fail"] is not None:
            raise w["fail"]  # type: ignore[misc]
        if path_kind == "channel":
            return {"id": channel_id, "topic": "Live topic"}
        if path_kind == "posts":
            return {"threads": [], "first_messages": []}
        return [
            {
                "id": "m1",
                "timestamp": "2026-10-01T00:00:00+00:00",
                "content": "hello",
                "author": {"id": "2", "username": "x"},
            }
        ]

    monkeypatch.setattr(server.discord, "channel", lambda cid: read("channel", cid))
    monkeypatch.setattr(server.discord, "latest_messages", lambda cid, n: read("messages", cid))
    monkeypatch.setattr(server.discord, "latest_posts", lambda cid, n: read("posts", cid))
    return w


def test_first_visit_offers_join_and_connect(client, world) -> None:
    state = client.get("/api/state").get_json()
    assert state["view"] == "offer"
    assert state["invite"] == "https://discord.gg/test"
    assert [c["name"] for c in state["channels"]] == ["general", "show-and-tell", "help-and-bugs"]
    assert state["account"] is None


def test_connect_files_one_request_then_waits_until_granted(client, world) -> None:
    assert client.post("/api/connect").get_json() == {"view": "waiting"}
    assert world["requests"] == [1]
    assert client.get("/api/state").get_json()["view"] == "waiting"
    world["granted"] = True
    state = client.get("/api/state").get_json()
    assert state["view"] == "connected" and state["account"]["name"] == "faraway9911"
    assert server.store.state()["pending_request"] is None


def test_connect_when_access_already_granted_files_nothing(client, world) -> None:
    world["granted"] = True
    client.post("/api/disconnect")
    assert client.get("/api/state").get_json()["view"] == "offer"
    assert client.post("/api/connect").get_json() == {"view": "connected"}
    assert world["requests"] == []


def test_cancel_returns_to_the_offer(client, world) -> None:
    client.post("/api/connect")
    client.post("/api/cancel")
    assert client.get("/api/state").get_json()["view"] == "offer"


def test_messages_read_only_the_studio_channels_and_keep_the_raw_records(client, world) -> None:
    world["granted"] = True
    data = client.get("/api/messages").get_json()
    assert data["joined"] and data["problem"] is None
    assert {cid for _, cid in world["reads"]} == config.CHANNEL_IDS  # type: ignore[union-attr]
    general = data["channels"][0]
    assert general["topic"] == "Live topic" and general["items"][0]["text"] == "hello"
    kept = server.store.messages(config.CHANNELS[0].id)
    assert kept["items"]["m1"]["content"] == "hello" and kept["loaded_at"]


def test_not_a_member_shows_join(client, world) -> None:
    world["fail"] = HttpError(403, '{"code": 50001}', "u")
    assert client.get("/api/messages").get_json() == {"joined": False, "channels": []}


def test_discord_down_keeps_what_was_last_read(client, world) -> None:
    client.get("/api/messages")
    world["fail"] = UnreachableError("down")
    data = client.get("/api/messages").get_json()
    assert data["problem"] == "unreachable"
    assert data["channels"][0]["items"][0]["text"] == "hello"
    assert data["loaded_at"]


def test_uninstall_takes_the_icons_off_and_undo_puts_them_back(client, world) -> None:
    world["granted"] = True
    assert client.post("/api/uninstall").get_json() == {"view": "uninstalled"}
    assert client.get("/api/state").get_json()["view"] == "uninstalled"
    assert client.post("/api/undo-uninstall").get_json() == {"view": "offer"}
    assert world["restored"] == [{"desktop_id": "home", "shortcut": {}}]
    # Undo leaves the user disconnected; reading again is one click.
    assert client.get("/api/state").get_json()["view"] == "offer"


def test_raw_shows_only_studio_channels(client, world) -> None:
    client.get("/api/messages")
    page = client.get(f"/raw/{config.CHANNELS[0].id}")
    assert page.status_code == 200 and "hello" in page.get_data(as_text=True)
    assert client.get("/raw/1515031776420954112").status_code == 404


def test_the_page_and_its_script_are_served(client) -> None:
    assert "Talk to the Studio team" in client.get("/").get_data(as_text=True)
    assert client.get("/app.js").mimetype == "text/javascript"


def test_out_of_scope_guard_is_what_the_routes_use() -> None:
    assert server.discord is discord
