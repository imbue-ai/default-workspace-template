"""Chat updates: the line AI Usage Limits puts into a chat when a limit goes up, only with the setting on."""

import json
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from ai_usage import runner
from ai_usage.chat_updates import (
    NOTE_SOURCE,
    Settings,
    SettingsStore,
    chat_app_url,
    increase_text,
    post_note,
    send_notes,
)
from ai_usage.provider import Limit
from ai_usage.providers import claude
from ai_usage.usage import AgentInfo, Source, UsageStore

WORKING_CHAT = "agent-00000000000000000000000000000001"
IDLE_CHAT = "agent-00000000000000000000000000000002"
OTHER_ACCOUNT_CHAT = "agent-00000000000000000000000000000003"


def _limit(key: str, label: str, percent: float) -> Limit:
    return Limit(key=key, label=label, percent=percent, resets_at=None)


# ----- the line -----


def test_a_limit_that_went_up_is_named_with_its_new_whole_percent() -> None:
    before = [_limit("five_hour", "Current session", 1.0), _limit("seven_day", "Weekly", 14.0)]
    after = [_limit("five_hour", "Current session", 2.0), _limit("seven_day", "Weekly", 14.2)]
    assert increase_text("Claude", before, after) == "Claude usage 2% (Current session)"


def test_several_limits_that_went_up_share_one_line() -> None:
    before = [_limit("five_hour", "Current session", 1.0), _limit("seven_day", "Weekly", 14.0)]
    after = [_limit("five_hour", "Current session", 2.0), _limit("seven_day", "Weekly", 15.0)]
    assert increase_text("Claude", before, after) == "Claude usage 2% (Current session), 15% (Weekly)"


def test_no_line_when_no_limit_crossed_a_whole_percent_or_one_went_down() -> None:
    before = [_limit("five_hour", "Current session", 10.1), _limit("seven_day", "Weekly", 40.0)]
    after = [_limit("five_hour", "Current session", 10.4), _limit("seven_day", "Weekly", 2.0)]
    assert increase_text("Claude", before, after) is None
    # A limit the earlier reading did not have is not an increase.
    assert increase_text("Claude", [], [_limit("five_hour", "Current session", 30.0)]) is None


def test_a_rise_that_rounds_up_to_the_next_percent_counts() -> None:
    before = [_limit("five_hour", "Current session", 10.4)]
    after = [_limit("five_hour", "Current session", 10.6)]
    assert increase_text("Claude", before, after) == "Claude usage 11% (Current session)"


# ----- the setting -----


def test_chat_updates_are_off_until_turned_on_and_the_choice_is_kept(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path)
    assert store.read() == Settings(chat_updates=False)

    store.write(Settings(chat_updates=True))

    assert SettingsStore(tmp_path).read().chat_updates is True
    assert json.loads((tmp_path / "settings.json").read_text()) == {"chat_updates": True}
    assert not (tmp_path / "settings.tmp").exists()


def test_an_unreadable_settings_file_reads_as_off(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text("{not json")
    assert SettingsStore(tmp_path).read().chat_updates is False
    (tmp_path / "settings.json").write_text('{"chat_updates": "maybe"}')
    assert SettingsStore(tmp_path).read().chat_updates is False


@pytest.fixture
def isolated_settings(tmp_path: Path) -> Iterator[SettingsStore]:
    """The page's settings routes, pointed at a scratch settings file for the test."""
    original = runner.store.settings
    runner.store.settings = SettingsStore(tmp_path)
    try:
        yield runner.store.settings
    finally:
        runner.store.settings = original


def test_the_page_reads_and_turns_chat_updates_on_and_off(isolated_settings: SettingsStore) -> None:
    client = runner.app.test_client()
    assert client.get("/api/settings").get_json() == {"chat_updates": False}

    turned_on = client.put("/api/settings", json={"chat_updates": True})
    assert (turned_on.status_code, turned_on.get_json()) == (200, {"chat_updates": True})
    assert isolated_settings.read().chat_updates is True
    assert client.get("/api/settings").get_json() == {"chat_updates": True}

    client.put("/api/settings", json={"chat_updates": False})
    assert isolated_settings.read().chat_updates is False


def test_a_setting_that_is_not_a_yes_or_no_is_refused(isolated_settings: SettingsStore) -> None:
    response = runner.app.test_client().put("/api/settings", json={"chat_updates": "maybe"})
    assert response.status_code == 400
    assert isolated_settings.read().chat_updates is False


# ----- reaching the chat app -----


def test_the_chat_app_is_found_in_the_app_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry = tmp_path / "apps.toml"
    registry.write_text(
        '[[apps]]\nname = "files"\nurl = "http://localhost:9000"\n\n'
        '[[apps]]\nname = "chat"\nurl = "http://localhost:8011/"\n'
    )
    monkeypatch.setenv("MINDS_APPS_FILE", str(registry))
    assert chat_app_url() == "http://localhost:8011"


def test_the_chat_app_falls_back_to_its_usual_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINDS_APPS_FILE", str(tmp_path / "missing.toml"))
    assert chat_app_url() == "http://127.0.0.1:8010"
    (tmp_path / "missing.toml").write_text('[[apps]]\nname = "files"\nurl = "http://localhost:9000"\n')
    assert chat_app_url() == "http://127.0.0.1:8010"
    (tmp_path / "missing.toml").write_text("not = [toml")
    assert chat_app_url() == "http://127.0.0.1:8010"


class _FakeChatApp:
    """A stand-in chat app on a free port: records each note it is given, and answers with ``status``."""

    def __init__(self, status: int = 200) -> None:
        self.received: list[tuple[str, dict[str, Any]]] = []
        self.got_one = threading.Event()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                fake.received.append((self.path, json.loads(self.rfile.read(length))))
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")
                fake.got_one.set()

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"


@pytest.fixture
def chat_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeChatApp]:
    fake = _FakeChatApp()
    registry = tmp_path / "apps.toml"
    registry.write_text(f'[[apps]]\nname = "chat"\nurl = "{fake.url}"\n')
    monkeypatch.setenv("MINDS_APPS_FILE", str(registry))
    try:
        yield fake
    finally:
        fake.server.shutdown()


def test_a_note_goes_to_the_chats_notes_route_naming_this_app(chat_app: _FakeChatApp) -> None:
    assert post_note(WORKING_CHAT, "Claude usage 2% (Current session)") is True
    assert chat_app.received == [
        (
            f"/api/chats/{WORKING_CHAT}/app-notes",
            {"source": NOTE_SOURCE, "text": "Claude usage 2% (Current session)"},
        )
    ]


def test_sending_to_each_chat_happens_off_the_callers_thread(chat_app: _FakeChatApp) -> None:
    send_notes([WORKING_CHAT], "Claude usage 2%")
    assert chat_app.got_one.wait(timeout=5.0)
    assert [path for path, _ in chat_app.received] == [f"/api/chats/{WORKING_CHAT}/app-notes"]


def test_a_chat_app_that_refuses_or_is_not_there_is_logged_never_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    refusing = _FakeChatApp(status=404)
    registry = tmp_path / "apps.toml"
    try:
        registry.write_text(f'[[apps]]\nname = "chat"\nurl = "{refusing.url}"\n')
        monkeypatch.setenv("MINDS_APPS_FILE", str(registry))
        assert post_note(WORKING_CHAT, "Claude usage 2%") is False
    finally:
        refusing.server.shutdown()
        refusing.server.server_close()
    # Nothing listens there any more.
    assert post_note(WORKING_CHAT, "Claude usage 2%") is False


# ----- a reading that rose -----


class _FakeUsageEndpoint:
    """Answers the usage endpoint the way Claude's does, with whatever ``utilization`` the test sets."""

    def __init__(self) -> None:
        self.utilization = 0.0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                body = json.dumps(
                    {"five_hour": {"utilization": fake.utilization, "resets_at": "2026-11-18T15:00:00+00:00"}}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()


@pytest.fixture
def usage_endpoint() -> Iterator[_FakeUsageEndpoint]:
    fake = _FakeUsageEndpoint()
    try:
        yield fake
    finally:
        fake.server.shutdown()


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _store_reading_through(
    tmp_path: Path, endpoint: _FakeUsageEndpoint, chat_updates: bool
) -> tuple[UsageStore, Source, list[tuple[list[str], str]]]:
    """A store for one Claude account with three chats -- one working right now, one idle for hours, one on another
    account and working -- whose notes are recorded rather than sent, with the given setting."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    store = UsageStore(data_dir)
    sent: list[tuple[list[str], str]] = []
    store._send_notes = lambda chat_ids, text: sent.append((list(chat_ids), text))
    store.settings.write(Settings(chat_updates=chat_updates))
    store._agents = [
        AgentInfo(chat_id=WORKING_CHAT, name="Working", agent_type="claude", account="acct"),
        AgentInfo(chat_id=IDLE_CHAT, name="Idle", agent_type="claude", account="acct"),
        AgentInfo(chat_id=OTHER_ACCOUNT_CHAT, name="Elsewhere", agent_type="claude", account="other"),
    ]
    store._agents_at = time.time() + 3600
    now = datetime.now(timezone.utc)
    activity = [
        {"t": _iso(now - timedelta(hours=5)), "chat_id": IDLE_CHAT, "event": "prompt", "prompt": "old work"},
        {"t": _iso(now - timedelta(hours=5) + timedelta(minutes=1)), "chat_id": IDLE_CHAT, "event": "stop"},
        {"t": _iso(now - timedelta(minutes=2)), "chat_id": WORKING_CHAT, "event": "prompt", "prompt": "do it"},
        {"t": _iso(now - timedelta(minutes=2)), "chat_id": OTHER_ACCOUNT_CHAT, "event": "prompt", "prompt": "x"},
    ]
    store.activity_path.write_text("".join(json.dumps(record) + "\n" for record in activity))
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps({"claudeAiOauth": {"accessToken": "token"}}))
    provider = claude.PROVIDER.model_copy(
        update={
            "usage_url": f"http://127.0.0.1:{endpoint.server.server_port}/",
            "credentials_paths": lambda account: [credentials],
        }
    )
    source = Source(key="claude:acct", provider=provider, account="acct", account_label="acct")
    return store, source, sent


def _earlier_reading(store: UsageStore, utilization: float, minutes_ago: float, status: int = 200) -> None:
    record: dict[str, Any] = {
        "t": _iso(datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)),
        "status": status,
        "source_key": "claude:acct",
    }
    if status == 200:
        record["raw"] = {"five_hour": {"utilization": utilization, "resets_at": "2026-11-18T15:00:00+00:00"}}
    with store.readings_path.open("a") as log:
        log.write(json.dumps(record) + "\n")


def test_a_rise_puts_a_line_into_the_chats_that_were_working_on_that_account(
    tmp_path: Path, usage_endpoint: _FakeUsageEndpoint
) -> None:
    store, source, sent = _store_reading_through(tmp_path, usage_endpoint, chat_updates=True)
    _earlier_reading(store, 10.0, minutes_ago=3)
    usage_endpoint.utilization = 12.0

    record = store.take_reading(source)

    assert record is not None and record["status"] == 200
    assert sent == [([WORKING_CHAT], "Claude usage 12% (Current session)")]


def test_no_line_while_chat_updates_are_off(tmp_path: Path, usage_endpoint: _FakeUsageEndpoint) -> None:
    store, source, sent = _store_reading_through(tmp_path, usage_endpoint, chat_updates=False)
    _earlier_reading(store, 10.0, minutes_ago=3)
    usage_endpoint.utilization = 12.0

    store.take_reading(source)

    assert sent == []


def test_no_line_when_usage_did_not_go_up(tmp_path: Path, usage_endpoint: _FakeUsageEndpoint) -> None:
    store, source, sent = _store_reading_through(tmp_path, usage_endpoint, chat_updates=True)
    _earlier_reading(store, 10.0, minutes_ago=3)
    usage_endpoint.utilization = 10.0

    store.take_reading(source)

    assert sent == []


def test_no_line_for_the_first_reading_there_is(tmp_path: Path, usage_endpoint: _FakeUsageEndpoint) -> None:
    store, source, sent = _store_reading_through(tmp_path, usage_endpoint, chat_updates=True)
    usage_endpoint.utilization = 12.0

    store.take_reading(source)

    assert sent == []


def test_a_rise_is_measured_from_the_last_reading_that_worked(
    tmp_path: Path, usage_endpoint: _FakeUsageEndpoint
) -> None:
    store, source, sent = _store_reading_through(tmp_path, usage_endpoint, chat_updates=True)
    _earlier_reading(store, 10.0, minutes_ago=3)
    _earlier_reading(store, 0.0, minutes_ago=1, status=429)
    usage_endpoint.utilization = 11.0

    store.take_reading(source)

    assert sent == [([WORKING_CHAT], "Claude usage 11% (Current session)")]


def test_no_chat_gets_the_line_when_none_was_working_between_the_readings(
    tmp_path: Path, usage_endpoint: _FakeUsageEndpoint
) -> None:
    store, source, sent = _store_reading_through(tmp_path, usage_endpoint, chat_updates=True)
    # Only the idle chat's stretch, hours before the earlier reading, is left.
    idle_only = [line for line in store.activity_path.read_text().splitlines() if IDLE_CHAT in line]
    store.activity_path.write_text("\n".join(idle_only) + "\n")
    _earlier_reading(store, 10.0, minutes_ago=3)
    usage_endpoint.utilization = 12.0

    store.take_reading(source)

    assert [chat_ids for chat_ids, _ in sent] == [[]]


def test_sending_to_no_chats_posts_nothing(chat_app: _FakeChatApp) -> None:
    send_notes([], "Claude usage 2%")
    assert not chat_app.got_one.wait(timeout=0.2)
    assert chat_app.received == []
