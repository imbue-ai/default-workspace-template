"""In-mind Claude authentication recovery: status checks, OAuth PTY flow, API-key write.

Implements the backend half of the in-UI Claude login modal so that a user
whose Claude credentials didn't sync into the mind can recover without
dropping into the ttyd terminal.

Three sign-in paths:

1. Subscription OAuth (`claude auth login --claudeai`) and Console OAuth
   (`claude auth login --console`) are driven via pexpect: the CLI prints a
   `claude.ai/oauth/authorize` URL and waits for a `CODE#STATE` paste on
   stdin. The PTY subprocess is held in module state between the
   `start_oauth_login` and `submit_oauth_code` calls so the UI can collect
   the code from the user in between.
2. Raw API key: `submit_api_key` writes `ANTHROPIC_API_KEY` into the host
   env file the bootstrap already manages and then restarts the named
   chat agent via `mngr stop`/`mngr start` so the new env is in effect
   the next time claude is launched.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import uuid as _uuid
from enum import Enum
from pathlib import Path
from typing import Final

import pexpect
from loguru import logger as _loguru_logger
from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.utils.env_utils import parse_env_file

logger = _loguru_logger

_HOST_DIR_ENV_VAR = "MNGR_HOST_DIR"
_ANTHROPIC_API_KEY_ENV_VAR = "ANTHROPIC_API_KEY"
_OAUTH_URL_REGEX = re.compile(r"https://claude\.ai/oauth/authorize\S*")
_OAUTH_URL_WAIT_SECONDS: Final = 30.0
_OAUTH_COMPLETE_WAIT_SECONDS: Final = 30.0
_MNGR_COMMAND_TIMEOUT_SECONDS: Final = 60.0
_CLAUDE_AUTH_STATUS_TIMEOUT_SECONDS: Final = 10.0


class ClaudeAuthError(RuntimeError):
    """Raised when an auth flow operation cannot complete."""


class AuthStatus(FrozenModel):
    """Parsed output of `claude auth status --json`.

    `subscription_type` is unset for Console accounts (API-usage billing),
    so the frontend conditionally renders the success-state copy.
    """

    logged_in: bool = Field(description="Whether claude is currently authenticated")
    auth_method: str | None = Field(default=None, description="e.g. 'oauth', 'api_key'")
    api_provider: str | None = Field(default=None, description="e.g. 'anthropic', 'claudeai'")
    email: str | None = Field(default=None)
    org_id: str | None = Field(default=None)
    org_name: str | None = Field(default=None)
    subscription_type: str | None = Field(default=None, description="e.g. 'Max'; absent for Console accounts")


class OAuthProvider(str, Enum):
    CLAUDEAI = "claudeai"
    CONSOLE = "console"


class OAuthStartResult(FrozenModel):
    session_id: str = Field(description="Opaque token for the in-flight OAuth session")
    oauth_url: str = Field(description="URL the user opens to authorize the login")


def _coerce_str_or_none(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    return str(value)


def _parse_status_payload(payload: dict[str, object]) -> AuthStatus:
    return AuthStatus(
        logged_in=bool(payload.get("loggedIn", False)),
        auth_method=_coerce_str_or_none(payload.get("authMethod")),
        api_provider=_coerce_str_or_none(payload.get("apiProvider")),
        email=_coerce_str_or_none(payload.get("email")),
        org_id=_coerce_str_or_none(payload.get("orgId")),
        org_name=_coerce_str_or_none(payload.get("orgName")),
        subscription_type=_coerce_str_or_none(payload.get("subscriptionType")),
    )


def get_auth_status() -> AuthStatus:
    """Invoke `claude auth status --json` and parse the result.

    A `claude` binary that isn't on PATH, times out, or returns no output
    is reported as `logged_in=False` rather than raising, since the whole
    point of the modal is to recover from broken auth state.
    """
    try:
        result = subprocess.run(
            ["claude", "auth", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=_CLAUDE_AUTH_STATUS_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning("claude auth status timed out; treating as logged out")
        return AuthStatus(logged_in=False)
    except FileNotFoundError:
        logger.warning("claude binary not found on PATH; treating as logged out")
        return AuthStatus(logged_in=False)

    stdout = result.stdout.strip()
    if not stdout:
        return AuthStatus(logged_in=False)
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise ClaudeAuthError(f"claude auth status returned non-JSON output: {stdout!r}") from e
    if not isinstance(payload, dict):
        raise ClaudeAuthError(f"claude auth status returned non-object JSON: {payload!r}")
    return _parse_status_payload(payload)


def _format_env_file(env: dict[str, str]) -> str:
    """Render an env dict back into the host env file format (matches mngr's _format_env_file)."""
    lines: list[str] = []
    for key, value in env.items():
        if " " in value or '"' in value or "'" in value or "\n" in value:
            value = '"' + value.replace('"', '\\"') + '"'
        lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def _resolve_host_env_path() -> Path:
    host_dir = os.environ.get(_HOST_DIR_ENV_VAR, "")
    if not host_dir:
        raise ClaudeAuthError(f"{_HOST_DIR_ENV_VAR} is unset; cannot locate host env file")
    return Path(host_dir) / "env"


def write_api_key_to_host_env(api_key: SecretStr, env_path_override: Path | None = None) -> Path:
    """Persist `ANTHROPIC_API_KEY=<value>` into the host env file (idempotent).

    Mirrors the host-env-write pattern used by the bootstrap for
    `CLAUDE_CONFIG_DIR`. The host env is sourced when an agent's tmux
    session starts, so a `mngr stop`/`mngr start` of the chat agent
    afterwards picks the new key up.
    """
    env_path = env_path_override or _resolve_host_env_path()
    existing: dict[str, str] = {}
    if env_path.exists():
        existing = parse_env_file(env_path.read_text())
    existing[_ANTHROPIC_API_KEY_ENV_VAR] = api_key.get_secret_value()
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(_format_env_file(existing))
    return env_path


def restart_agent(agent_name: str) -> None:
    """Restart a chat agent via `mngr stop` then `mngr start`.

    Tmux session contents are lost, but the agent re-launches with a fresh
    env (so newly-written `ANTHROPIC_API_KEY` is in effect).
    """
    logger.info("Restarting agent {} via mngr stop+start", agent_name)
    stop_result = subprocess.run(
        ["mngr", "stop", agent_name],
        capture_output=True,
        text=True,
        timeout=_MNGR_COMMAND_TIMEOUT_SECONDS,
        check=False,
    )
    if stop_result.returncode != 0:
        raise ClaudeAuthError(
            f"mngr stop {agent_name} failed (exit {stop_result.returncode}): {stop_result.stderr.strip()}"
        )
    start_result = subprocess.run(
        ["mngr", "start", agent_name],
        capture_output=True,
        text=True,
        timeout=_MNGR_COMMAND_TIMEOUT_SECONDS,
        check=False,
    )
    if start_result.returncode != 0:
        raise ClaudeAuthError(
            f"mngr start {agent_name} failed (exit {start_result.returncode}): {start_result.stderr.strip()}"
        )


def submit_api_key(api_key: SecretStr, chat_agent_name: str | None) -> AuthStatus:
    """Write `ANTHROPIC_API_KEY` to host env then restart the chat agent."""
    write_api_key_to_host_env(api_key)
    if chat_agent_name:
        restart_agent(chat_agent_name)
    return get_auth_status()


# ---- OAuth PTY flow ----


class _OAuthSession:
    """Holds a live `claude auth login` PTY subprocess between `/start` and `/submit-code`."""

    def __init__(self, provider: OAuthProvider) -> None:
        self.session_id = _uuid.uuid4().hex
        self.provider = provider
        self.process: pexpect.spawn = pexpect.spawn(
            "claude",
            ["auth", "login", f"--{provider.value}"],
            timeout=_OAUTH_URL_WAIT_SECONDS,
            encoding="utf-8",
        )
        match_index = self.process.expect([_OAUTH_URL_REGEX, pexpect.EOF, pexpect.TIMEOUT])
        if match_index != 0:
            self.terminate()
            if match_index == 1:
                raise ClaudeAuthError("claude auth login exited before printing OAuth URL")
            raise ClaudeAuthError("Timed out waiting for OAuth URL from claude auth login")
        match = self.process.match
        if match is None:
            self.terminate()
            raise ClaudeAuthError("OAuth URL regex matched but pexpect.match is None (unexpected)")
        self.oauth_url = match.group(0)

    def submit_code(self, code: str) -> None:
        self.process.timeout = _OAUTH_COMPLETE_WAIT_SECONDS
        self.process.sendline(code)
        result = self.process.expect([pexpect.EOF, pexpect.TIMEOUT])
        if result != 0:
            raise ClaudeAuthError("Timed out waiting for claude auth login to complete after code submit")

    def terminate(self) -> None:
        if not self.process.isalive():
            return
        try:
            self.process.terminate(force=True)
        except OSError as e:
            logger.warning("OAuth subprocess terminate raised: {}", e)


_oauth_lock = threading.Lock()
_current_oauth: _OAuthSession | None = None


def start_oauth_login(provider: OAuthProvider) -> OAuthStartResult:
    """Spawn `claude auth login --<provider>` and return the parsed OAuth URL.

    Replaces any prior in-flight session: only one OAuth flow can be live
    at a time per process, which matches the single-mind / single-user
    deployment model.
    """
    global _current_oauth
    with _oauth_lock:
        if _current_oauth is not None:
            _current_oauth.terminate()
            _current_oauth = None
        session = _OAuthSession(provider)
        _current_oauth = session
    return OAuthStartResult(session_id=session.session_id, oauth_url=session.oauth_url)


def submit_oauth_code(session_id: str, code: str) -> AuthStatus:
    """Send the user's pasted `CODE#STATE` to the live OAuth subprocess."""
    global _current_oauth
    with _oauth_lock:
        session = _current_oauth
        if session is None or session.session_id != session_id:
            raise ClaudeAuthError("No active OAuth session matches the provided session_id")
        try:
            session.submit_code(code)
        finally:
            _current_oauth = None
    return get_auth_status()


def abort_oauth_login() -> None:
    """Drop any in-flight OAuth session (e.g. user closed the modal)."""
    global _current_oauth
    with _oauth_lock:
        if _current_oauth is not None:
            _current_oauth.terminate()
            _current_oauth = None
