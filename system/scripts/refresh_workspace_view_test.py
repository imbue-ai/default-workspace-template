"""Tests for the agent-facing refresh_workspace_view.py helper.

These tests exercise the contract the update flows depend on:

- The view epoch is bumped on disk on every run, before anything is asked of
  the network. It is the only channel that survives the interface being down,
  and the only one that reaches a viewer who is not looking right now.
- The two live channels fire independently -- the WebSocket broadcast reaches
  shared tunnel viewers the Minds app cannot, and the app call drops a cache the
  broadcast cannot touch.
- One channel failing never suppresses another, and no failure is fatal: a stale
  tab must not fail a reveal whose change already landed on disk.
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


@pytest.fixture
def epoch_path(tmp_path: Path) -> Path:
    """Where a run under test records the new interface epoch."""
    return tmp_path / "state" / "view_epoch"


def test_refresh_fires_every_channel(epoch_path: Path) -> None:
    """No channel alone reaches every viewer, so a run must fire all of them."""
    http = _RecordingHttp({})

    exit_code = refresh_workspace_view.refresh(
        runner=_StubRunner(), http=http, base_url=_BASE_URL, epoch_path=epoch_path
    )

    assert exit_code == 0
    assert epoch_path.read_text().strip() != ""
    assert http.url_containing("/api/layout/broadcast") is not None
    assert http.url_containing(f"/agents/{_PRIMARY_ID}/refresh") is not None


def test_each_run_records_a_distinct_epoch(epoch_path: Path) -> None:
    """A second reveal must be distinguishable from the first.

    A page that reloaded for reveal N holds N; if reveal N+1 wrote the same
    value, that page would have no way to know it is stale again.
    """
    refresh_workspace_view.refresh(
        runner=_StubRunner(),
        http=_RecordingHttp({}),
        base_url=_BASE_URL,
        epoch_path=epoch_path,
    )
    first = epoch_path.read_text()

    refresh_workspace_view.refresh(
        runner=_StubRunner(),
        http=_RecordingHttp({}),
        base_url=_BASE_URL,
        epoch_path=epoch_path,
    )

    assert epoch_path.read_text() != first


def test_epoch_is_recorded_even_when_nothing_is_reachable(epoch_path: Path) -> None:
    """The durable channel must not depend on anything being up.

    This is the case the helper exists for: callers run it straight after a
    services restart, so the server may still be down and the app unreachable.
    The epoch is what makes the reload happen anyway, when the browser
    reconnects.
    """
    http = _RecordingHttp({"broadcast": None, "refresh": None})

    exit_code = refresh_workspace_view.refresh(
        runner=_StubRunner(), http=http, base_url=_BASE_URL, epoch_path=epoch_path
    )

    assert exit_code == 0
    assert epoch_path.read_text().strip() != ""


def test_epoch_is_recorded_before_the_live_channels(epoch_path: Path) -> None:
    """Ordering matters: a POST can block for its full timeout.

    If the epoch were written after, a browser reconnecting during that window
    would read the old value and stay on the previous build.
    """
    written_when_posting: list[bool] = []

    class _ObservingHttp(_RecordingHttp):
        def post_json(
            self, url: str, payload: dict, headers: dict, timeout: float
        ) -> int | None:
            written_when_posting.append(epoch_path.exists())
            return super().post_json(url, payload, headers, timeout)

    refresh_workspace_view.refresh(
        runner=_StubRunner(),
        http=_ObservingHttp({}),
        base_url=_BASE_URL,
        epoch_path=epoch_path,
    )

    assert written_when_posting == [True, True]


def test_broadcast_asks_for_a_whole_interface_reload(epoch_path: Path) -> None:
    """The op must be the full-UI reload, not the per-iframe ``refresh``.

    Only the top-level reload picks up new hashed assets and shell chrome.
    """
    http = _RecordingHttp({})

    refresh_workspace_view.refresh(
        runner=_StubRunner(), http=http, base_url=_BASE_URL, epoch_path=epoch_path
    )

    broadcasts = [p for p in http.posts if "/api/layout/broadcast" in p[0]]
    assert [payload["op"] for _url, payload, _headers in broadcasts] == [
        "reload_system_interface"
    ]


def test_app_refresh_targets_the_primary_agent_not_the_caller(epoch_path: Path) -> None:
    """A sub-agent must refresh its workspace's window, not its own agent id.

    The Minds app identifies a workspace window by the primary agent's id, so a
    refresh addressed to a sub-agent names no window at all.
    """
    http = _RecordingHttp({})

    refresh_workspace_view.refresh(
        runner=_StubRunner(), http=http, base_url=_BASE_URL, epoch_path=epoch_path
    )

    assert http.url_containing(f"/agents/{_PRIMARY_ID}/refresh") is not None
    assert http.url_containing(f"/agents/{_OWN_ID}/refresh") is None


def test_unresolvable_primary_skips_the_app_call_rather_than_guessing(
    epoch_path: Path,
) -> None:
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
        epoch_path=epoch_path,
    )

    assert http.url_containing("/refresh") is None
    assert http.url_containing("/api/layout/broadcast") is not None


def test_primary_lookup_ignores_a_nonzero_exit(epoch_path: Path) -> None:
    """A discovery error prints to stdout too; only a clean exit is trusted."""
    http = _RecordingHttp({})

    refresh_workspace_view.refresh(
        runner=_StubRunner(stdout="Error: could not reach provider\n", returncode=1),
        http=http,
        base_url=_BASE_URL,
        epoch_path=epoch_path,
    )

    assert http.url_containing("/refresh") is None


def test_a_failed_broadcast_still_refreshes_the_app(epoch_path: Path) -> None:
    """The channels are independent: a dead frontend WebSocket is exactly the
    case the app call exists to cover, so it must still run."""
    http = _RecordingHttp({"/api/layout/broadcast": None})

    exit_code = refresh_workspace_view.refresh(
        runner=_StubRunner(), http=http, base_url=_BASE_URL, epoch_path=epoch_path
    )

    assert exit_code == 0
    assert http.url_containing(f"/agents/{_PRIMARY_ID}/refresh") is not None


def test_a_failed_app_refresh_still_broadcasts(epoch_path: Path) -> None:
    """A workspace with no desktop app attached still has tunnel viewers to reload."""
    http = _RecordingHttp({"/refresh": 502})

    exit_code = refresh_workspace_view.refresh(
        runner=_StubRunner(), http=http, base_url=_BASE_URL, epoch_path=epoch_path
    )

    assert exit_code == 0
    assert http.url_containing("/api/layout/broadcast") is not None


def test_refresh_is_never_fatal_when_every_channel_fails(tmp_path: Path) -> None:
    """The change already landed on disk; a stale tab must not fail the reveal."""
    unwritable_epoch = tmp_path / "not-a-directory" / "view_epoch"
    unwritable_epoch.parent.write_text("this is a file, not a directory")
    http = _RecordingHttp({"broadcast": None, "refresh": None})

    exit_code = refresh_workspace_view.refresh(
        runner=_StubRunner(), http=http, base_url=_BASE_URL, epoch_path=unwritable_epoch
    )

    assert exit_code == 0


def test_missing_gateway_env_skips_the_app_call_but_still_broadcasts(
    monkeypatch: pytest.MonkeyPatch, epoch_path: Path
) -> None:
    """A bare mngr workspace (no desktop app) still reloads its browser viewers.

    The lookup must not even run there: it is a 30s-budget subprocess with
    nothing to address.
    """
    monkeypatch.delenv("LATCHKEY_GATEWAY", raising=False)
    http = _RecordingHttp({})
    runner = _StubRunner()

    exit_code = refresh_workspace_view.refresh(
        runner=runner, http=http, base_url=_BASE_URL, epoch_path=epoch_path
    )

    assert exit_code == 0
    assert runner.commands == []
    assert http.url_containing("/api/layout/broadcast") is not None
    assert http.url_containing("/refresh") is None
