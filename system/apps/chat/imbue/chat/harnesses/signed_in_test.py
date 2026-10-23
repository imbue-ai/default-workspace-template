"""Tests for the promote gate.

This module decides whether a folder a sign-in just wrote to actually holds a working
credential, and it is the only thing standing between "the file was written" and "the
account is offered to the user". Every arm below is a judgement about a CLI's output, so
the runner is injected rather than shelling out to whatever binaries this machine has.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from imbue.chat.harnesses.grok.testing import GROK_SESSION_SCOPE
from imbue.chat.harnesses.grok.testing import build_grok_key_scope
from imbue.chat.harnesses.grok.testing import write_grok_auth
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.signed_in import SignedIn
from imbue.chat.harnesses.signed_in import is_signed_in
from imbue.concurrency_group.errors import ProcessError


class _Finished:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "", is_timed_out: bool = False) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.is_timed_out = is_timed_out


def _runner_returning(finished: _Finished, seen: dict[str, Any] | None = None):
    def run(**kwargs: Any) -> _Finished:
        if seen is not None:
            seen.update(kwargs)
        return finished

    return run


# pi: no exit code to read, so the string is the whole test


def test_pi_reads_its_actual_signed_out_message(tmp_path: Path) -> None:
    """Measured against 0.83.0, 0.84.1 and 0.87.1: an empty dir, an unknown provider id and a
    malformed auth.json all print this and exit ZERO. Matching anything else makes NO
    unreachable and the gate a round trip that always says yes."""
    runner = _runner_returning(
        _Finished(returncode=0, stdout="No models available. Use /login to log into a provider")
    )
    assert is_signed_in(HarnessType.PI_CODING, tmp_path, runner) is SignedIn.NO


def test_pi_naming_models_is_a_working_credential(tmp_path: Path) -> None:
    runner = _runner_returning(_Finished(returncode=0, stdout="groq/llama-3.3-70b\ngroq/qwen"))
    assert is_signed_in(HarnessType.PI_CODING, tmp_path, runner) is SignedIn.YES


# claude: the server's own credentials must not answer for the folder


def test_the_probe_does_not_carry_the_servers_own_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`claude auth status --json` reports loggedIn on an ambient ANTHROPIC_API_KEY alone, so
    an upgraded workspace's leftover key would commit an EMPTY account folder as signed in,
    make it the most-recently-used, and launch every later chat with no credential."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-leftover")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-leftover")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    seen: dict[str, Any] = {}
    runner = _runner_returning(_Finished(returncode=0), seen)

    is_signed_in(HarnessType.CLAUDE, tmp_path, runner)

    env = seen["env"]
    assert "ANTHROPIC_API_KEY" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert env["PATH"] == "/usr/bin:/bin", "the ambient env is layered under, not replaced"
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path)


def test_claude_and_codex_are_decided_by_the_exit_code(tmp_path: Path) -> None:
    for harness in (HarnessType.CLAUDE, HarnessType.CODEX):
        assert is_signed_in(harness, tmp_path, _runner_returning(_Finished(returncode=0))) is SignedIn.YES
        assert is_signed_in(harness, tmp_path, _runner_returning(_Finished(returncode=1))) is SignedIn.NO


# the three-way answer


def test_a_probe_that_cannot_run_is_unknown_not_signed_out(tmp_path: Path) -> None:
    """Collapsing this into NO would delete a folder the user just completed a browser OAuth
    into, because the probes shell out to CLIs that fetch over the network."""

    def run(**_kwargs: Any) -> _Finished:
        raise ProcessError(command=("claude",), stdout="", stderr="no such binary")

    assert is_signed_in(HarnessType.CLAUDE, tmp_path, run) is SignedIn.UNKNOWN


def test_a_timeout_is_unknown(tmp_path: Path) -> None:
    runner = _runner_returning(_Finished(is_timed_out=True))
    assert is_signed_in(HarnessType.ANTIGRAVITY, tmp_path, runner) is SignedIn.UNKNOWN


def test_agy_failing_for_any_other_reason_is_unknown(tmp_path: Path) -> None:
    """agy fetches the catalogue over the network, so a non-zero exit means "signed out OR
    the network blinked". Its text distinguishes them; without the guard a network failure
    would fall through to YES and commit a signed-out account."""
    runner = _runner_returning(_Finished(returncode=1, stderr="Error: connection reset"))
    assert is_signed_in(HarnessType.ANTIGRAVITY, tmp_path, runner) is SignedIn.UNKNOWN


def test_agy_saying_sign_in_is_signed_out(tmp_path: Path) -> None:
    runner = _runner_returning(_Finished(returncode=1, stderr="Please sign in to view available models."))
    assert is_signed_in(HarnessType.ANTIGRAVITY, tmp_path, runner) is SignedIn.NO


def test_a_harness_with_no_probe_is_taken_at_its_word(tmp_path: Path) -> None:
    def run(**_kwargs: Any) -> _Finished:
        raise AssertionError("nothing to ask")

    assert is_signed_in(HarnessType.OPENCODE, tmp_path, run) is SignedIn.YES


# grok: its banner for a session login, xAI itself for a stored API key

# What `grok models` printed on the pinned 1.0.62 binary, for a session login and for none.
_GROK_SIGNED_IN_OUTPUT = "You are logged in with grok.com.\n\nDefault model: grok-4.6\n"
_GROK_SIGNED_OUT_OUTPUT = "You are not authenticated.\n\nDefault model: grok-4.6\n"


def _key_only_account(account: Path, key: str = "xai-key-5c1e") -> Path:
    write_grok_auth(account, {"xai::api_key": build_grok_key_scope(key)})
    return account


def _no_runner(**_kwargs: Any) -> _Finished:
    raise AssertionError("`grok models` never reads a stored API key, so it must not be asked about one")


def _no_fetcher(_request: httpx.Request) -> httpx.Response:
    raise AssertionError("only an API key is asked of xAI")


def _fetcher_answering(response: httpx.Response, seen: list[httpx.Request] | None = None):
    def fetch(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return response

    return fetch


def test_a_grok_key_is_checked_with_xai_the_way_grok_checks_it(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    fetcher = _fetcher_answering(httpx.Response(200, json={"api_key_id": "k", "api_key_blocked": False}), seen)

    assert is_signed_in(HarnessType.GROK, _key_only_account(tmp_path), _no_runner, fetcher) is SignedIn.YES

    (request,) = seen
    assert (request.method, str(request.url)) == ("GET", "https://api.x.ai/v1/api-key")
    assert request.headers["Authorization"] == "Bearer xai-key-5c1e"


@pytest.mark.parametrize(
    ("response", "verdict"),
    (
        pytest.param(httpx.Response(200, json={"api_key_blocked": True}), SignedIn.NO, id="200-blocked"),
        pytest.param(httpx.Response(200, json={"api_key_disabled": True}), SignedIn.NO, id="200-disabled"),
        pytest.param(httpx.Response(200, json={"team_blocked": True}), SignedIn.NO, id="200-team-blocked"),
        pytest.param(httpx.Response(200, json={"api_key_blocked": "true"}), SignedIn.YES, id="200-flag-not-a-bool"),
        pytest.param(httpx.Response(200, content=b"not json"), SignedIn.YES, id="200-unparseable"),
        pytest.param(httpx.Response(200, json=["not", "an", "object"]), SignedIn.YES, id="200-not-an-object"),
        pytest.param(httpx.Response(400, json={"error": "Incorrect API key"}), SignedIn.NO, id="400"),
        pytest.param(httpx.Response(401), SignedIn.NO, id="401"),
        pytest.param(httpx.Response(402), SignedIn.NO, id="402"),
        pytest.param(httpx.Response(403), SignedIn.NO, id="403"),
        pytest.param(httpx.Response(404), SignedIn.UNKNOWN, id="404"),
        pytest.param(httpx.Response(429), SignedIn.UNKNOWN, id="429"),
        pytest.param(httpx.Response(500), SignedIn.UNKNOWN, id="500"),
        pytest.param(httpx.Response(503), SignedIn.UNKNOWN, id="503"),
    ),
)
def test_a_grok_key_is_judged_as_grok_judges_it(tmp_path: Path, response: httpx.Response, verdict: SignedIn) -> None:
    """grok's own api_key_probe: an auth error or a blocked, disabled or team-blocked key is unusable; any other 200,
    an unparseable one included, is usable; the rest fails open, which the paste flow commits on."""
    fetcher = _fetcher_answering(response)
    assert is_signed_in(HarnessType.GROK, _key_only_account(tmp_path), _no_runner, fetcher) is verdict


def test_a_grok_key_probe_that_cannot_reach_xai_is_unknown(tmp_path: Path) -> None:
    def fetch(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    assert is_signed_in(HarnessType.GROK, _key_only_account(tmp_path), _no_runner, fetch) is SignedIn.UNKNOWN


@pytest.mark.parametrize("key", ("", "   ", "xai-é", "xai-a\nb"), ids=("empty", "blank", "non-ascii", "newline"))
def test_a_grok_key_no_request_can_carry_is_refused_without_asking(tmp_path: Path, key: str) -> None:
    assert is_signed_in(HarnessType.GROK, _key_only_account(tmp_path, key), _no_runner, _no_fetcher) is SignedIn.NO


@pytest.mark.parametrize(
    ("finished", "verdict"),
    (
        (_Finished(stdout=_GROK_SIGNED_IN_OUTPUT), SignedIn.YES),
        (_Finished(stdout=_GROK_SIGNED_OUT_OUTPUT), SignedIn.NO),
        # The banner reads the file; a model listing the network then fails does not unsay it.
        (
            _Finished(returncode=1, stdout="You are logged in with grok.com.\n", stderr="Error: timed out"),
            SignedIn.YES,
        ),
        (_Finished(returncode=1, stderr="Error: connection reset"), SignedIn.UNKNOWN),
        (_Finished(stdout="Model 'mine' is using its own API key.\n"), SignedIn.UNKNOWN),
        (_Finished(is_timed_out=True), SignedIn.UNKNOWN),
    ),
    ids=("signed-in", "signed-out", "signed-in-then-error", "error", "other-banner", "timeout"),
)
def test_a_grok_session_login_is_judged_by_the_grok_models_banner(
    tmp_path: Path, finished: _Finished, verdict: SignedIn
) -> None:
    write_grok_auth(tmp_path, {GROK_SESSION_SCOPE: {"key": "session-token", "auth_mode": "oidc"}})
    seen: dict[str, Any] = {}

    assert is_signed_in(HarnessType.GROK, tmp_path, _runner_returning(finished, seen), _no_fetcher) is verdict
    assert seen["command"] == ["grok", "models"]


@pytest.mark.parametrize(
    ("scopes", "banner", "verdict"),
    (
        # The session is what grok's turns use first, so it is the session that has to work.
        pytest.param(
            {GROK_SESSION_SCOPE: {"key": "session-token"}, "xai::api_key": build_grok_key_scope()},
            _GROK_SIGNED_IN_OUTPUT,
            SignedIn.YES,
            id="session-beside-key",
        ),
        # Measured on 1.0.62: a key scope without `create_time` and `user_id` makes grok take the whole file as
        # empty, so a key xAI would accept is still no credential to the agents.
        pytest.param(
            {"xai::api_key": {"key": "xai-key-5c1e", "auth_mode": "api_key"}},
            _GROK_SIGNED_OUT_OUTPUT,
            SignedIn.NO,
            id="unreadable-key",
        ),
        pytest.param(None, _GROK_SIGNED_OUT_OUTPUT, SignedIn.NO, id="no-credential-file"),
    ),
)
def test_a_grok_account_without_a_sole_readable_key_is_judged_by_the_banner_not_by_xai(
    tmp_path: Path, scopes: dict[str, object] | None, banner: str, verdict: SignedIn
) -> None:
    if scopes is not None:
        write_grok_auth(tmp_path, scopes)
    runner = _runner_returning(_Finished(stdout=banner))
    assert is_signed_in(HarnessType.GROK, tmp_path, runner, _no_fetcher) is verdict


def test_the_grok_probe_does_not_carry_the_servers_own_credentials_endpoints_or_overlays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each of these would make `grok models` answer for something other than the folder: an env key or a
    provider command is a login of its own, a base URL sends the question elsewhere, and an overlay or a
    pointer to another home or credential file reads other settings."""
    server_variables = {
        "XAI_API_KEY": "xai-server-key",
        "GROK_CODE_XAI_API_KEY": "xai-legacy-key",
        "GROK_AUTH": '{"key": "token"}',
        "GROK_AUTH_PROVIDER_COMMAND": "/usr/local/bin/print-token",
        "GROK_DEPLOYMENT_KEY": "deploy-key",
        "GROK_XAI_API_BASE_URL": "http://127.0.0.1:9/v1",
        "GROK_MODELS_BASE_URL": "http://127.0.0.1:9/v1",
        "GROK_CLI_CHAT_PROXY_BASE_URL": "http://127.0.0.1:9/v1",
        "GROK_WS_ORIGIN": "https://elsewhere.example",
        "GROK_CONFIG": "/tmp/overlay.toml",
        "GROK_CONFIG_PATH": "/tmp/overlay.toml",
        "GROK_AGENT": "1",
        "GROK_HOME": "/home/user/.grok",
        "GROK_AUTH_PATH": "/home/user/.grok/auth.json",
    }
    for name, value in server_variables.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    seen: dict[str, Any] = {}

    is_signed_in(HarnessType.GROK, tmp_path, _runner_returning(_Finished(stdout=_GROK_SIGNED_OUT_OUTPUT), seen))

    env = seen["env"]
    assert env["GROK_HOME"] == str(tmp_path)
    assert env["GROK_AUTH_PATH"] == str(tmp_path / "auth.json")
    assert env["GROK_MANAGED_CONFIG"] == "0", "the account's pinned home keeps its requirements.toml"
    assert (set(server_variables) - {"GROK_HOME", "GROK_AUTH_PATH"}).isdisjoint(env)
    assert env["PATH"] == "/usr/bin:/bin", "the ambient env is layered under, not replaced"
