"""Tests for the reload_interface.py reveal-step helper.

These tests exercise the behavior the lead agent depends on when it runs the
frontend reveal step:

- ``main`` POSTs ``{op: "reload_interface", args: {}, agent_id}`` to the
  loopback ``/api/layout/broadcast`` endpoint, with the agent id riding both
  the body and the ``X-Mngr-Agent-Id`` header, against the configured
  workspace URL.
- A successful broadcast exits 0.
- An HTTP error response and an unreachable server both exit 1, mirroring the
  ``scripts/layout.py`` transport-status contract.
"""

from __future__ import annotations

import importlib.util
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).parent / "reload_interface.py"
_spec = importlib.util.spec_from_file_location("reload_interface", _SCRIPT)
assert _spec is not None and _spec.loader is not None
reload_interface = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reload_interface)


class _FakeResponse:
    def __init__(self, text: str, status: int = 200) -> None:
        self.status = status
        self._text = text

    def read(self) -> bytes:
        return self._text.encode("utf-8")

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def test_main_posts_reload_op_with_agent_id_and_exits_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reachable server yields a reload_interface broadcast and exit code 0."""
    monkeypatch.setenv(reload_interface.ENV_MNGR_AGENT_ID, "agent-42")
    monkeypatch.setenv(reload_interface.ENV_WORKSPACE_URL, "http://127.0.0.1:8000")

    captured: dict[str, Any] = {}

    def fake_urlopen(req: urllib.request.Request, timeout: float) -> _FakeResponse:
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = dict(req.header_items())
        captured["body"] = req.data
        return _FakeResponse('{"ok": true}')

    monkeypatch.setattr(reload_interface.urllib.request, "urlopen", fake_urlopen)

    assert reload_interface.main() == 0
    assert captured["url"] == "http://127.0.0.1:8000/api/layout/broadcast"
    assert captured["method"] == "POST"
    # urllib title-cases header names in header_items().
    assert captured["headers"]["X-mngr-agent-id"] == "agent-42"
    assert json.loads(captured["body"]) == {
        "op": "reload_interface",
        "args": {},
        "agent_id": "agent-42",
    }


def test_main_returns_error_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """An HTTP error response from the endpoint exits 1."""

    def fake_urlopen(req: urllib.request.Request, timeout: float) -> _FakeResponse:
        raise urllib.error.HTTPError(
            url=req.full_url,
            code=400,
            msg="Bad Request",
            hdrs=None,  # type: ignore[arg-type]
            fp=None,
        )

    monkeypatch.setattr(reload_interface.urllib.request, "urlopen", fake_urlopen)

    assert reload_interface.main() == 1


def test_main_returns_error_when_server_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A connection failure (URLError) exits 1."""

    def fake_urlopen(req: urllib.request.Request, timeout: float) -> _FakeResponse:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(reload_interface.urllib.request, "urlopen", fake_urlopen)

    assert reload_interface.main() == 1
