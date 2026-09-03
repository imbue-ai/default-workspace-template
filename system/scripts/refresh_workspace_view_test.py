"""Tests for the agent-facing refresh_workspace_view.py helper.

These tests exercise the contract the update flows depend on:

- Both channels fire on every run, independently -- the WebSocket broadcast
  reaches shared tunnel viewers the Minds app cannot, and the app call reaches
  the desktop app without going through the workspace server at all.
- One channel failing never suppresses the other, and no failure is fatal:
  a stale tab must not fail a reveal whose change already landed on disk.
- The app call addresses the workspace's PRIMARY agent, and is skipped outright
  rather than aimed at the caller when that cannot be resolved.
- The app call carries the per-agent override JWT when the gateway issues one and
  goes out without it when the gateway does not, so neither gateway topology
  loses the channel.
"""

from __future__ import annotations

import importlib.util
import subprocess
from http.client import InvalidURL
from pathlib import Path
from typing import Any
from typing import Sequence

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
    """Records every POST and answers each URL from a caller-supplied status map.

    ``error_by_url_fragment`` raises instead of answering, standing in for the
    escapes ``post_json``'s own ``except`` clauses do not cover.
    """

    def __init__(
        self,
        status_by_url_fragment: dict[str, int | None],
        error_by_url_fragment: dict[str, Exception] | None = None,
    ) -> None:
        self._status_by_url_fragment = status_by_url_fragment
        self._error_by_url_fragment = error_by_url_fragment or {}
        self.posts: list[tuple[str, dict, dict]] = []

    def post_json(
        self, url: str, payload: dict, headers: dict, timeout: float
    ) -> int | None:
        self.posts.append((url, payload, headers))
        for fragment, error in self._error_by_url_fragment.items():
            if fragment in url:
                raise error
        for fragment, status in self._status_by_url_fragment.items():
            if fragment in url:
                return status
        return 200

    def url_containing(self, fragment: str) -> str | None:
        for url, _payload, _headers in self.posts:
            if fragment in url:
                return url
        return None

    def headers_of(self, fragment: str) -> dict:
        """The headers of the first POST whose URL contains ``fragment``.

        Raises rather than returning ``None`` so a caller asserting that some
        header is *absent* cannot pass by never having found the request.
        """
        for url, _payload, headers in self.posts:
            if fragment in url:
                return headers
        raise AssertionError(f"no POST was made to a URL containing {fragment!r}")


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
    """A fully-wired workspace on a desktop-hosted gateway.

    That topology is the one that carries an override JWT; a test covering the
    VPS shape drops it.
    """
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


def test_app_refresh_sends_the_override_when_the_gateway_issues_one() -> None:
    """A desktop-hosted gateway authorizes the call by this JWT and nothing else.

    Without the header it resolves the request against its deny-all default
    permissions file, so dropping it 403s every refresh from a local workspace.
    """
    http = _RecordingHttp({})

    refresh_workspace_view.refresh(runner=_StubRunner(), http=http, base_url=_BASE_URL)

    headers = http.headers_of("/refresh")
    assert headers["X-Latchkey-Gateway-Permissions-Override"] == "jwt"


def test_app_refresh_still_runs_when_the_gateway_issues_no_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A VPS-hosted gateway injects no override, and must not lose this channel.

    Only a desktop-hosted gateway mints the per-agent JWT; a VPS-hosted one
    forwards Minds API routes to the desktop and substitutes a target JWT of its
    own, so the variable is simply absent on a remote workspace. Treating that as
    "no app attached" would leave every remote workspace with just the broadcast
    -- the channel the services restart has usually already disconnected.
    """
    monkeypatch.delenv("LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE", raising=False)
    http = _RecordingHttp({})

    refresh_workspace_view.refresh(runner=_StubRunner(), http=http, base_url=_BASE_URL)

    headers = http.headers_of("/refresh")
    assert "X-Latchkey-Gateway-Permissions-Override" not in headers
    assert headers["X-Latchkey-Gateway-Password"] == "secret"


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


def test_primary_lookup_uses_an_id_listed_alongside_a_provider_error() -> None:
    """A partial listing still names the primary, so the app call must still run.

    ``mngr ls`` prints every agent it did list to stdout, then exits non-zero if
    any provider errored -- with the error block on stderr. An unconfigured cloud
    provider is routine, so gating on the exit code would disable the app channel
    on hosts where the id was right there.
    """
    http = _RecordingHttp({})

    refresh_workspace_view.refresh(
        runner=_StubRunner(stdout=f"{_PRIMARY_ID}\n", returncode=1),
        http=http,
        base_url=_BASE_URL,
    )

    assert http.url_containing(f"/agents/{_PRIMARY_ID}/refresh") is not None


def test_primary_lookup_skips_the_app_call_when_nothing_was_listed() -> None:
    """A listing that named no primary is not something to guess around."""
    http = _RecordingHttp({})

    refresh_workspace_view.refresh(
        runner=_StubRunner(stdout="", returncode=1), http=http, base_url=_BASE_URL
    )

    assert http.url_containing("/refresh") is None


def test_primary_lookup_selects_by_is_primary_alone() -> None:
    """The query must not re-acquire a ``workspace`` label conjunct.

    The Minds app stopped setting that label on its agents, so a query carrying
    it matches nothing in any real workspace -- the lookup silently resolves
    nobody and the app channel goes dark. Nothing else pins this argv.
    """
    http = _RecordingHttp({})
    runner = _StubRunner()

    refresh_workspace_view.refresh(runner=runner, http=http, base_url=_BASE_URL)

    assert runner.commands == [["mngr", "ls", "--include", "has(labels.is_primary)", "--ids"]]


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


def test_an_unexpected_error_is_reported_rather_than_raised() -> None:
    """The always-exits-0 contract holds for errors the channels do not catch.

    Both channels have escapes outside the exception groups they handle (a
    malformed gateway URL, non-UTF-8 subprocess output). The update flows tell
    the agent a non-zero exit is never a reason to stop, so a traceback escaping
    here would read as a failed reveal for a change already on disk.
    """
    http = _RecordingHttp({})

    exit_code = refresh_workspace_view.refresh(
        runner=_StubRunner(error=ValueError("nonnumeric port")),
        http=http,
        base_url=_BASE_URL,
    )

    assert exit_code == 0


def test_an_unexpected_broadcast_error_still_refreshes_the_app() -> None:
    """The escape guard is per-channel, so the first channel cannot cancel the second.

    The broadcast runs first, and a malformed ``MINDS_WORKSPACE_SERVER_URL``
    raises ``http.client.InvalidURL`` -- an ``HTTPException``, not an
    ``OSError``, so ``post_json`` does not catch it. Guarding both channels
    together would swallow that *and* skip the app call, taking out the one
    channel that reaches the common case (a user watching in the Minds app)
    while still exiting 0, so the caller reads it as a routine miss.
    """
    http = _RecordingHttp(
        {}, {"/api/layout/broadcast": InvalidURL("nonnumeric port: 'notaport'")}
    )

    exit_code = refresh_workspace_view.refresh(
        runner=_StubRunner(), http=http, base_url=_BASE_URL
    )

    assert exit_code == 0
    assert http.url_containing(f"/agents/{_PRIMARY_ID}/refresh") is not None


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
