"""Tests for the datalib skill's sync_request.py, against a real HTTP server standing in for datalib-http."""

from __future__ import annotations

import importlib.util
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).parent / "sync_request.py"
_spec = importlib.util.spec_from_file_location("sync_request", _SCRIPT)
assert _spec is not None and _spec.loader is not None
sync_request = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync_request)

_TOKEN = "token.0123_abc~"


class _FakeDatalib:
    """What the fake server answers, and what it was asked."""

    def __init__(self) -> None:
        # One entry per GET /api/requests, served in order; the last one repeats.
        self.listings: list[list[dict[str, Any]]] = [[]]
        self.opened: list[dict[str, Any]] = []
        self.posted_bodies: list[dict[str, Any]] = []
        self.authorizations: list[str | None] = []
        self.gets = 0


@pytest.fixture
def fake() -> Iterator[tuple[_FakeDatalib, str]]:
    state = _FakeDatalib()

    class Handler(BaseHTTPRequestHandler):
        def _answer(self, status: int, payload: Any) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            state.authorizations.append(self.headers.get("Authorization"))
            if self.headers.get("Authorization") == f"Bearer {_TOKEN}":
                return True
            self._answer(401, {"error": "no token"})
            return False

        def do_GET(self) -> None:
            if not self._authorized():
                return
            listing = state.listings[min(state.gets, len(state.listings) - 1)]
            state.gets += 1
            self._answer(200, listing)

        def do_POST(self) -> None:
            if not self._authorized():
                return
            state.posted_bodies.append(
                json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            )
            self._answer(200, state.opened)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield state, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    token_file = tmp_path / "system" / "api-token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text(f"{_TOKEN}\n")
    return tmp_path


def _request(request_id: str, state: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": request_id,
        "roots": ["slack/ingest"],
        "by": "agent",
        "state": state,
        **extra,
    }


def _run(origin: str, data_root: Path, *args: str) -> int:
    return sync_request.main(["--origin", origin, "--data-root", str(data_root), *args])


@pytest.fixture(autouse=True)
def no_waiting_between_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync_request, "POLL_INTERVAL_SECONDS", 0.0)


def test_open_files_the_roots_and_prints_each_request_id(
    fake: tuple[_FakeDatalib, str], data_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state, origin = fake
    state.opened = [_request("r1", "open"), _request("r2", "open")]

    assert (
        _run(
            origin, data_root, "open", "slack/ingest", "github/ingest", "--by", "chat-7"
        )
        == 0
    )

    assert state.posted_bodies == [
        {"roots": ["slack/ingest", "github/ingest"], "by": "chat-7"}
    ]
    assert capsys.readouterr().out == "r1\nr2\n"


def test_wait_polls_until_the_request_closes_and_reports_done(
    fake: tuple[_FakeDatalib, str], data_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state, origin = fake
    state.listings = [
        [_request("r1", "open")],
        [_request("r1", "open")],
        [_request("r1", "done")],
    ]

    assert _run(origin, data_root, "wait", "r1") == 0

    assert state.gets == 3
    assert capsys.readouterr().out == "r1  done  slack/ingest\n"


def test_wait_keeps_waiting_while_any_of_several_requests_is_open(
    fake: tuple[_FakeDatalib, str], data_root: Path
) -> None:
    state, origin = fake
    state.listings = [
        [_request("r1", "done"), _request("r2", "open")],
        [_request("r1", "done"), _request("r2", "done")],
    ]

    assert _run(origin, data_root, "wait", "r1", "r2") == 0
    assert state.gets == 2


def test_wait_reports_a_failure_with_the_step_and_datalib_dags_exit_code(
    fake: tuple[_FakeDatalib, str], data_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state, origin = fake
    state.listings = [
        [_request("r1", "failed", failed_step="slack/ingest"), _request("r2", "done")]
    ]

    assert _run(origin, data_root, "wait", "r1", "r2") == 2

    assert (
        "r1  failed  slack/ingest  (failed at slack/ingest)" in capsys.readouterr().out
    )


def test_wait_reports_a_stopped_request_and_who_stopped_it(
    fake: tuple[_FakeDatalib, str], data_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state, origin = fake
    state.listings = [[_request("r1", "stopped", stop_requested_by="ui")]]

    assert _run(origin, data_root, "wait", "r1") == 130

    assert "(stopped by ui)" in capsys.readouterr().out


def test_a_request_the_server_no_longer_lists_is_not_reported_as_success(
    fake: tuple[_FakeDatalib, str], data_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Guards the silent success: an id missing from the listing has an outcome this script cannot see."""
    state, origin = fake
    state.listings = [[_request("other", "done")]]

    assert _run(origin, data_root, "wait", "r1") == 1

    assert "r1  not listed" in capsys.readouterr().out


def test_a_missing_token_file_says_to_start_the_app(
    fake: tuple[_FakeDatalib, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _state, origin = fake

    assert _run(origin, tmp_path / "no-root", "wait", "r1") == 1

    assert "install_datalib.sh" in capsys.readouterr().err


def test_a_refused_token_is_an_error_at_once_for_open(
    fake: tuple[_FakeDatalib, str], data_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _state, origin = fake
    (data_root / "system" / "api-token").write_text("wrong")

    assert _run(origin, data_root, "open", "slack/ingest") == 1

    assert "answered 401" in capsys.readouterr().err


def test_wait_rides_out_a_server_that_is_briefly_unreachable() -> None:
    answers: list[Any] = [
        sync_request.SyncRequestError("connection refused"),
        sync_request.SyncRequestError("connection refused"),
        [_request("r1", "done")],
    ]

    def fetch(path: str, body: bytes | None) -> Any:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    states = sync_request.wait_for_requests(
        fetch, ["r1"], clock=lambda: 0.0, sleep=lambda _seconds: None
    )

    assert states["r1"]["state"] == "done"


def test_wait_gives_up_once_the_server_has_been_unreachable_past_the_budget() -> None:
    now = [0.0]

    def fetch(path: str, body: bytes | None) -> Any:
        raise sync_request.SyncRequestError("connection refused")

    def sleep(_seconds: float) -> None:
        now[0] += sync_request.UNREACHABLE_BUDGET_SECONDS / 2

    with pytest.raises(sync_request.SyncRequestError, match="has not answered for"):
        sync_request.wait_for_requests(fetch, ["r1"], clock=lambda: now[0], sleep=sleep)
