"""Tests for the agent-facing refresh_workspace_view.py helper.

These tests exercise the contract the update flows depend on:

- Both channels fire on every run, independently -- the WebSocket broadcast
  reaches shared tunnel viewers the Minds app cannot, and the app call drops a
  cache and survives a dead WebSocket the broadcast needs.
- One channel failing never suppresses the other, and no failure is fatal:
  a stale tab must not fail a reveal whose change already landed on disk.
- The app call addresses the workspace's PRIMARY agent, not the caller, so a
  sub-agent refreshes the right window.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from typing import Any, Sequence

import pytest

_SCRIPT = Path(__file__).parent / "refresh_workspace_view.py"
_spec = importlib.util.spec_from_file_location("refresh_workspace_view", _SCRIPT)
assert _spec is not None and _spec.loader is not None
refresh_workspace_view = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(refresh_workspace_view)

_PRIMARY_ID = "agent-primary"
_OWN_ID = "agent-subagent"
_BASE_URL = "http://127.0.0.1:8000"


class _RecordingHttp(refresh_workspace_view.HttpClient):
    """Records every POST and answers each URL from a caller-supplied status map."""

    def __init__(self, status_by_url_fragment: dict[str, int | None]) -> None:
        self._status_by_url_fragment = status_by_url_fragment
        self.posts: list[tuple[str, dict, dict]] = []

    def post_json(
        self, url: str, payload: dict, headers: dict, timeout: float
    ) -> int | None:
        self.posts.append((url, payload, headers))
        for fragment, status in self._status_by_url_fragment.items():
            if fragment in url:
                return status
        return 200

    def url_containing(self, fragment: str) -> str | None:
        for url, _payload, _headers in self.posts:
            if fragment in url:
                return url
        return None


class _StubRunner(refresh_workspace_view.Runner):
    """Answers ``mngr ls`` with a fixed result (or raises, for the lookup-failed path)."""

    def __init__(
        self,
        stdout: str = f"{_PRIMARY_ID}\n",
        returncode: int = 0,
        error: Exception | None = None,
    ) -> None:
        self._stdout = stdout
        self._returncode = returncode
        self._error = error
        self.commands: list[list[str]] = []

    def run(self, argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess:
        self.commands.append(list(argv))
        if self._error is not None:
            raise self._error
        return subprocess.CompletedProcess(
            list(argv), self._returncode, self._stdout, ""
        )


@pytest.fixture(autouse=True)
def _agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fully-wired workspace: our own agent id plus reachable gateway creds."""
    monkeypatch.setenv("MNGR_AGENT_ID", _OWN_ID)
    monkeypatch.setenv("LATCHKEY_GATEWAY", "http://gateway.invalid")
    monkeypatch.setenv("LATCHKEY_GATEWAY_PASSWORD", "secret")
    monkeypatch.setenv("LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE", "jwt")


def test_refresh_fires_both_channels() -> None:
    """Neither channel alone reaches every viewer, so a run must fire both."""
    http = _RecordingHttp({})

    exit_code = refresh_workspace_view.refresh(
        runner=_StubRunner(), http=http, base_url=_BASE_URL
    )

    assert exit_code == 0
    broadcast_url = http.url_containing("/api/layout/broadcast")
    app_url = http.url_containing("/minds-api-proxy/")
    assert broadcast_url == f"{_BASE_URL}/api/layout/broadcast"
    assert (
        app_url
        == f"http://gateway.invalid/minds-api-proxy/api/v1/agents/{_PRIMARY_ID}/refresh"
    )


def test_broadcast_asks_for_a_whole_interface_reload() -> None:
    """The op must be the full-UI reload, not the per-iframe ``refresh``.

    ``refresh`` only reloads inner panels; a backend restart invalidates the
    shell itself, so anything short of the full reload leaves stale code running.
    """
    http = _RecordingHttp({})

    refresh_workspace_view.refresh(runner=_StubRunner(), http=http, base_url=_BASE_URL)

    broadcast_payload = next(
        p for url, p, _h in http.posts if "/api/layout/broadcast" in url
    )
    assert broadcast_payload["op"] == "reload_system_interface"


def test_app_refresh_targets_the_primary_agent_not_the_caller() -> None:
    """A sub-agent must refresh its workspace's window, not its own agent id.

    The Minds app keys windows by primary agent id, so addressing our own id
    would refresh some other window or none at all.
    """
    http = _RecordingHttp({})

    refresh_workspace_view.refresh(runner=_StubRunner(), http=http, base_url=_BASE_URL)

    app_url = http.url_containing("/minds-api-proxy/")
    assert app_url is not None
    assert _PRIMARY_ID in app_url
    assert _OWN_ID not in app_url


def test_a_failed_broadcast_still_refreshes_the_app() -> None:
    """The channels are independent: a dead frontend WebSocket is exactly the
    case the app call exists to cover, so it must still run."""
    http = _RecordingHttp({"/api/layout/broadcast": None})

    exit_code = refresh_workspace_view.refresh(
        runner=_StubRunner(), http=http, base_url=_BASE_URL
    )

    assert exit_code == 0
    assert http.url_containing("/minds-api-proxy/") is not None


def test_a_failed_app_refresh_still_broadcasts() -> None:
    """A workspace with no desktop app attached still has tunnel viewers to reload."""
    http = _RecordingHttp({"/minds-api-proxy/": 502})

    exit_code = refresh_workspace_view.refresh(
        runner=_StubRunner(), http=http, base_url=_BASE_URL
    )

    assert exit_code == 0
    assert http.url_containing("/api/layout/broadcast") is not None


def test_refresh_is_never_fatal_when_both_channels_fail() -> None:
    """The change already landed on disk; a stale tab must not fail the reveal."""
    http = _RecordingHttp({"/api/layout/broadcast": None, "/minds-api-proxy/": None})

    exit_code = refresh_workspace_view.refresh(
        runner=_StubRunner(), http=http, base_url=_BASE_URL
    )

    assert exit_code == 0


def test_missing_gateway_env_skips_the_app_call_but_still_broadcasts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare mngr workspace (no desktop app) still reloads its browser viewers."""
    monkeypatch.delenv("LATCHKEY_GATEWAY", raising=False)
    http = _RecordingHttp({})

    exit_code = refresh_workspace_view.refresh(
        runner=_StubRunner(), http=http, base_url=_BASE_URL
    )

    assert exit_code == 0
    assert http.url_containing("/api/layout/broadcast") is not None
    assert http.url_containing("/minds-api-proxy/") is None


def test_primary_lookup_failure_falls_back_to_our_own_agent_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When ``mngr ls`` cannot run, the caller is usually the primary itself.

    The fallback must also say so: the POST succeeds either way, so without a
    note a sub-agent that just refreshed some other window sees only the
    "requested a reload" line and has no way to tell.
    """
    runner = _StubRunner(error=OSError("mngr not found"))
    http = _RecordingHttp({})

    refresh_workspace_view.refresh(runner=runner, http=http, base_url=_BASE_URL)

    app_url = http.url_containing("/minds-api-proxy/")
    assert app_url is not None
    assert _OWN_ID in app_url
    assert (
        "could not resolve this workspace's primary agent id" in capsys.readouterr().err
    )


def test_primary_lookup_ignores_a_nonzero_exit() -> None:
    """A discovery error prints to stdout too; only a clean exit is trusted."""
    runner = _StubRunner(stdout="Error: discovery failed\n", returncode=1)
    http = _RecordingHttp({})

    refresh_workspace_view.refresh(runner=runner, http=http, base_url=_BASE_URL)

    app_url = http.url_containing("/minds-api-proxy/")
    assert app_url is not None
    assert _OWN_ID in app_url
