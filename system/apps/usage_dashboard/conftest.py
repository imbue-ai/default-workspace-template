from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from flask import Flask
from flask import jsonify
from flask.testing import FlaskClient
from flask.typing import ResponseReturnValue

from usage_collector.feed import UsageFeed
from usage_collector.ingest import Ingestor
from usage_collector.server import create_app as create_collector_app
from usage_collector.status import IngestStatus
from usage_collector.store import UsageStore
from usage_dashboard.server import create_app


@pytest.fixture
def store(tmp_path: Path) -> Iterator[UsageStore]:
    usage_store = UsageStore(tmp_path / "usage.db")
    yield usage_store
    usage_store.close()


@pytest.fixture
def ingestor(store: UsageStore) -> Ingestor:
    return Ingestor(store, UsageFeed())


@pytest.fixture
def collector(store: UsageStore) -> Iterator[httpx.Client]:
    """A real collector app, reached over WSGI in this process."""
    collector_app = create_collector_app(store, UsageFeed(), IngestStatus())
    with httpx.Client(transport=httpx.WSGITransport(app=collector_app), base_url="http://collector") as client:
        yield client


@pytest.fixture
def static_directory(tmp_path: Path) -> Path:
    directory = tmp_path / "static"
    (directory / "assets").mkdir(parents=True)
    (directory / "index.html").write_text("<!doctype html><head><title>Usage</title></head><div id=app></div>")
    (directory / "assets" / "app-Ab12cd34.js").write_text("console.log('built');")
    return directory


@pytest.fixture
def apps_file(tmp_path: Path) -> Path:
    path = tmp_path / "apps.toml"
    path.write_text(
        '[[apps]]\nname = "files"\nlabel = "files-x1"\nurl = "http://localhost:8020"\ndisplay_name = "Files"\n'
        '[[apps]]\nname = "chat"\nlabel = "chat-x1"\nurl = "http://localhost:8010"\ndisplay_name = "Chat"\n'
    )
    return path


@pytest.fixture
def chat_app() -> Iterator[httpx.Client]:
    """A stand-in for the chat app at the URL ``apps_file`` registers it under, listing two chats."""
    chat = Flask("chat")

    @chat.get("/api/chats")
    def chats() -> ResponseReturnValue:
        return jsonify(
            chats=[{"chat_id": "agent-1", "title": "Seattle weather"}, {"chat_id": "agent-2", "title": "News"}]
        )

    with httpx.Client(mounts={"http://localhost:8010": httpx.WSGITransport(app=chat)}) as client:
        yield client


@pytest.fixture
def routines_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "routines"
    directory.mkdir()
    (directory / "us-news.toml").write_text(
        'title = "US news"\nwhat = "Pulls the latest US headlines."\nevery = "1h"\ncommand = ["true"]\n'
    )
    (directory / "broken.toml").write_text("title = [")
    return directory


@pytest.fixture
def client(
    static_directory: Path, collector: httpx.Client, apps_file: Path, chat_app: httpx.Client, routines_dir: Path
) -> FlaskClient:
    return create_app(static_directory, collector, apps_file, chat_app, routines_dir).test_client()


@pytest.fixture
def unreachable_client(static_directory: Path, apps_file: Path, tmp_path: Path) -> Iterator[FlaskClient]:
    """The app over a collector that refuses every connection."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with httpx.Client(transport=httpx.MockTransport(refuse), base_url="http://collector") as collector:
        yield create_app(static_directory, collector, apps_file, collector, tmp_path / "routines").test_client()
