"""Tests for the agent-facing refresh_workspace_view.py helper.

These tests exercise the contract the update flows depend on:

- Both channels fire on every run, independently -- the WebSocket broadcast
  reaches shared tunnel viewers the Minds app cannot, and the app call reaches
  the desktop app without going through the workspace server at all.
- One channel failing never suppresses the other, and no failure is fatal:
  a stale tab must not fail a reveal whose change already landed on disk.
- The app call addresses the workspace's PRIMARY agent, and is skipped outright
  rather than aimed at the caller when that cannot be resolved.
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
    assert http.url_containing("/api/layout/broadcast") is not None
    assert http.url_containing(f"/agents/{_PRIMARY_ID}/refresh") is not None


def test_broadcast_asks_for_a_whole_interface_reload() -> None:
    """The op must be the full-UI reload, not the per-iframe ``refresh``.

    Only the top-level reload picks up new hashed assets and shell chrome.
    """
    http = _RecordingHttp({})

    refresh_workspace_view.refresh(runner=_StubRunner(), http=http, base_url=_BASE_URL)

    broadcasts = [p for p in http.posts if "/api/layout/broadcast" in p[0]]
    assert [payload["op"] for _url, payload, _headers in broadcasts] == [
        "reload_system_interface"
    ]


def test_app_refresh_targets_the_primary_agent_not_the_caller() -> None:
    """A sub-agent must refresh its workspace's window, not its own agent id.

    The Minds app identifies a workspace window by the primary agent's id, so a
    refresh addressed to a sub-agent names no window at all.
    """
    http = _RecordingHttp({})

    refresh_workspace_view.refresh(runner=_StubRunner(), http=http, base_url=_BASE_URL)

    assert http.url_containing(f"/agents/{_PRIMARY_ID}/refresh") is not None
    assert http.url_containing(f"/agents/{_OWN_ID}/refresh") is None


def test_unresolvable_primary_skips_the_app_call_rather_than_guessing() -> None:
    """A lookup that fails must not fall back to the caller's own id.

    Addressing our own id would be accepted by the gateway and broadcast by the
    app, then match no window -- a silent no-op that reads as success. Skipping
    is the same outcome, reported honestly.
    """
    http = _RecordingHttp({})

    refresh_workspace_view.refresh(
        runner=_StubRunner(error=OSError("mngr not found")),
        http=http,
        base_url=_BASE_URL,
    )

    assert http.url_containing("/refresh") is None
    assert http.url_containing("/api/layout/broadcast") is not None


def test_primary_lookup_ignores_a_nonzero_exit() -> None:
    """A discovery error prints to stdout too; only a clean exit is trusted."""
    http = _RecordingHttp({})

    refresh_workspace_view.refresh(
        runner=_StubRunner(stdout="Error: could not reach provider\n", returncode=1),
        http=http,
        base_url=_BASE_URL,
    )

    assert http.url_containing("/refresh") is None


def test_a_failed_broadcast_still_refreshes_the_app() -> None:
    """The channels are independent, and the app call is the one that does not
    go through the workspace server -- so a server still coming back from the
    restart is exactly when it must still run."""
    http = _RecordingHttp({"/api/layout/broadcast": None})

    exit_code = refresh_workspace_view.refresh(
        runner=_StubRunner(), http=http, base_url=_BASE_URL
    )

    assert exit_code == 0
    assert http.url_containing(f"/agents/{_PRIMARY_ID}/refresh") is not None


def test_a_failed_app_refresh_still_broadcasts() -> None:
    """A workspace with no desktop app attached still has tunnel viewers to reload."""
    http = _RecordingHttp({"/refresh": 502})

    exit_code = refresh_workspace_view.refresh(
        runner=_StubRunner(), http=http, base_url=_BASE_URL
    )

    assert exit_code == 0
    assert http.url_containing("/api/layout/broadcast") is not None


def test_refresh_is_never_fatal_when_both_channels_fail() -> None:
    """The change already landed on disk; a stale tab must not fail the reveal."""
    http = _RecordingHttp({"broadcast": None, "refresh": None})

    exit_code = refresh_workspace_view.refresh(
        runner=_StubRunner(), http=http, base_url=_BASE_URL
    )

    assert exit_code == 0


def test_missing_gateway_env_skips_the_app_call_but_still_broadcasts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare mngr workspace (no desktop app) still reloads its browser viewers.

    The lookup must not even run there: it is a 30s-budget subprocess with
    nothing to address.
    """
    monkeypatch.delenv("LATCHKEY_GATEWAY", raising=False)
    http = _RecordingHttp({})
    runner = _StubRunner()

    exit_code = refresh_workspace_view.refresh(
        runner=runner, http=http, base_url=_BASE_URL
    )

    assert exit_code == 0
    assert runner.commands == []
    assert http.url_containing("/api/layout/broadcast") is not None
    assert http.url_containing("/refresh") is None
