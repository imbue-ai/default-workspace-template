"""In-mind Claude authentication: settings-env credential writes, setup-token flow, agent restarts.

Implements the backend half of the in-UI Claude login modal. All credentials
live in the ``env`` block of the shared ``$CLAUDE_CONFIG_DIR/settings.json``
(the config dir every claude in the mind inherits), NEVER in the mngr host
env file: the host env file is frozen into long-lived processes (supervisord
and its services) at boot, so changing it would require tearing down the
whole workspace, while a settings.json edit only requires restarting the
claude agents themselves.

Three sign-in paths, all converging on the same settings-env write:

1. Subscription: `claude setup-token` is driven via pexpect. The CLI prints
   an `oauth/authorize` URL and then *polls Anthropic itself*: once the user
   approves in the browser, the CLI prints the minted 1-year token without
   requiring a code paste (verified on the pinned Claude Code version; a
   `Paste code here if prompted >` fallback exists for flows that do demand
   one). The frontend polls `poll_setup_token` until the token appears; the
   token is written as ``CLAUDE_CODE_OAUTH_TOKEN``.
2. Raw API key: written as ``ANTHROPIC_API_KEY``.
3. Imbue (LiteLLM): an env-var-style blob pasted from the desktop app's
   mint page, written as ``ANTHROPIC_API_KEY`` + ``ANTHROPIC_BASE_URL``.

Paths 2 and 3 (and a subtle "paste an existing token" affordance) share one
strict env-lines parser: only the three managed keys are accepted, and
mixed-mode pastes (an OAuth token alongside an API key) are rejected so the
written state is always unambiguous. The writer fully controls the managed
keys -- switching modes deletes the other mode's keys.

Every successful write restarts the mind's claude-binary agents (types
``claude`` AND ``worker``; the ``main`` services agent is excluded -- its
window 0 never runs a live claude, and restarting it would tear down
supervisord and every background service). Settings-env values are read at
claude process start, so a restart is what makes new credentials take
effect. Agent states are snapshotted (via ``mngr list``) before stopping:
agents that were RUNNING mid-task get a "please continue" message after the
restart so unattended workers resume instead of silently dying; WAITING
agents need nothing (their next user message starts them with the fresh
env); STOPPED agents are left stopped.

Every restart first runs `_prepare_claude_config_for_restart`, which
pre-dismisses the Claude Code startup dialogs (onboarding, theme, custom
API-key challenge) in `.claude.json` so the freshly restarted agents come
up clean instead of blocking on an interactive TUI prompt -- mirroring
what mngr's claude plugin does at agent-creation time. The config edit
runs while every agent is stopped, so no still-running agent clobbers it
from its stale in-memory copy.

Dependencies that touch the outside world (subprocess invocation and
pexpect-driven PTY spawning) are injected into `ClaudeAuthService` at
construction so tests can substitute deterministic fakes without
`unittest.mock` or module-level monkeypatching.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import Any
from typing import Final

import pexpect
from loguru import logger as _loguru_logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import SecretStr

from imbue.concurrency_group.subprocess_utils import ProcessSetupError
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mngr.cli.exit_codes import EXIT_CODE_PROVIDER_INACCESSIBLE
from imbue.mngr.utils.env_utils import parse_env_file
from imbue.mngr_claude.claude_config import acknowledge_cost_threshold
from imbue.mngr_claude.claude_config import complete_onboarding
from imbue.mngr_claude.claude_config import dismiss_effort_callout
from imbue.mngr_claude.claude_config import read_claude_config
logger = _loguru_logger

_CLAUDE_CONFIG_DIR_ENV_VAR = "CLAUDE_CONFIG_DIR"
_HOST_DIR_ENV_VAR = "MNGR_HOST_DIR"
ANTHROPIC_API_KEY_ENV_VAR: Final[str] = "ANTHROPIC_API_KEY"
ANTHROPIC_BASE_URL_ENV_VAR: Final[str] = "ANTHROPIC_BASE_URL"
CLAUDE_CODE_OAUTH_TOKEN_ENV_VAR: Final[str] = "CLAUDE_CODE_OAUTH_TOKEN"
# The full set of settings-env keys this module owns. The writer enforces
# both presence AND absence: every write deletes all three before setting
# the submitted subset, so stale keys from a previous mode can never
# shadow the new one (ANTHROPIC_API_KEY outranks CLAUDE_CODE_OAUTH_TOKEN
# in Claude Code's credential precedence, so a leftover key would
# silently win over a freshly written token).
MANAGED_AUTH_ENV_KEYS: Final[frozenset[str]] = frozenset(
    (ANTHROPIC_API_KEY_ENV_VAR, ANTHROPIC_BASE_URL_ENV_VAR, CLAUDE_CODE_OAUTH_TOKEN_ENV_VAR)
)
# Claude stores per-key approvals keyed by the last 20 characters of the key.
_API_KEY_APPROVAL_SUFFIX_LENGTH: Final = 20
# Characters of the key/token shown in the modal's "currently signed in via"
# header; long enough to disambiguate, short enough to stay a non-secret.
_DISPLAY_SUFFIX_LENGTH: Final = 4
# Fires on the first sight of the OAuth URL in the PTY stream. This is only a
# *trigger*: the CLI's Ink renderer hard-wraps the visible URL at the terminal
# width (pexpect's default PTY is 80 columns) and pexpect can match mid
# render-frame, so the buffer may hold just a prefix. The actual URL is
# recovered by `_extract_oauth_url` after draining the stream.
_OAUTH_URL_REGEX = re.compile(r"https://\S*oauth/authorize\S*")
# An OSC 8 terminal hyperlink: `ESC ] 8 ; params ; target (BEL | ESC \)`.
# The params field is not always empty (the CLI emits `id=...`). The target
# carries the full URL with no width-wrapping, so it survives narrow PTYs
# that hard-wrap the visible label.
_OSC8_HYPERLINK_REGEX = re.compile(r"\x1b\]8;[^;\x07\x1b]*;([^\x07\x1b]+)(?:\x07|\x1b\\)")
# Strict charset for re-assembling a width-wrapped URL from visible text:
# unlike `\S`, it excludes stray control bytes left between render fragments.
_OAUTH_URL_CHARSET = r"[A-Za-z0-9%&=?_.~/:+#-]"
_OAUTH_URL_STRICT_REGEX = re.compile(rf"https://{_OAUTH_URL_CHARSET}*oauth/authorize{_OAUTH_URL_CHARSET}*")
_OAUTH_URL_CONTINUATION_REGEX = re.compile(rf"^{_OAUTH_URL_CHARSET}+$")
# Every terminal escape sequence (CSI, OSC, and stray two-byte escapes like
# cursor save/restore) plus non-newline control bytes. Replacing these with
# newlines -- instead of deleting them like `strip_ansi` -- keeps adjacent
# render fragments from being glued into one bogus run of text.
_TERMINAL_ESCAPE_OR_CONTROL_REGEX = re.compile(
    r"\x1b\[[0-9;]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b.|[\x00-\x08\x0b-\x1f\x7f]"
)
# The long-lived token `claude setup-token` prints on completion. Like the
# URL regex, only a trigger -- extraction re-assembles the possibly
# width-wrapped token from the drained stream.
_SETUP_TOKEN_REGEX = re.compile(r"sk-ant-oat01-[A-Za-z0-9_-]+")
_SETUP_TOKEN_STRICT_REGEX = re.compile(r"sk-ant-oat01-[A-Za-z0-9_-]*")
_SETUP_TOKEN_CONTINUATION_REGEX = re.compile(r"^[A-Za-z0-9_-]+$")
# Printed by the CLI when Anthropic rejects a pasted code (wrong, expired, or
# from an earlier attempt's state) or its own polling hits an error; the CLI
# then parks on a "Press Enter to retry." prompt, so without failing fast the
# session would just time out with a misleading message.
_OAUTH_ERROR_REGEX = re.compile(r"OAuth error")
# The CLI's Ink input treats a rapid burst of characters ending in a newline
# as pasted *content* -- the newline lands in the field instead of acting as
# the Enter keypress -- so the code and Enter must be sent as two separate
# writes with a pause in between (same pattern mngr uses to type into claude
# TUIs). Verified against the live CLI: `sendline` leaves the code sitting in
# the field forever; send + pause + CR submits it.
_SETUP_TOKEN_CODE_ENTER_DELAY_SECONDS: Final = 0.6
# Real setup tokens are ~110 characters. A much shorter extraction is a
# wrapped fragment, not the token -- keep waiting rather than storing it.
_MIN_SETUP_TOKEN_LENGTH: Final = 60
# After a trigger regex fires, keep draining the PTY until extraction yields
# a complete value; the spinner animates forever, so completion is judged by
# the caller's predicate with this hard deadline as backstop.
_STREAM_DRAIN_DEADLINE_SECONDS: Final = 6.0
_STREAM_DRAIN_READ_SECONDS: Final = 0.25
_OAUTH_URL_WAIT_SECONDS: Final = 30.0
_SETUP_TOKEN_POLL_SECONDS: Final = 0.2
_SETUP_TOKEN_CODE_WAIT_SECONDS: Final = 30.0
_MNGR_COMMAND_TIMEOUT_SECONDS: Final = 60.0
# Message delivery waits for durable submission evidence inside mngr, which
# polls the agent's transcript -- give it more headroom than plain commands.
_MNGR_MESSAGE_TIMEOUT_SECONDS: Final = 120.0
_CLAUDE_AUTH_STATUS_TIMEOUT_SECONDS: Final = 10.0

# Agent types whose window-0 process is a real claude binary and therefore
# holds credentials frozen from process start. The `main` services agent is
# deliberately absent: its window 0 sleeps forever and restarting it would
# tear down supervisord and every background service.
CLAUDE_BINARY_AGENT_TYPES: Final[frozenset[str]] = frozenset(("claude", "worker"))
_AGENT_STATE_RUNNING: Final[str] = "RUNNING"
_AGENT_STATE_WAITING: Final[str] = "WAITING"

# Sent (via `mngr message`) to agents that were RUNNING when the auth-change
# restart tore them down, so unattended work resumes instead of silently
# stopping. WAITING agents are not messaged: their next user message starts
# them under the fresh env anyway.
RESTART_CONTINUE_MESSAGE: Final[str] = (
    "Your Claude credentials were just updated and your session was restarted. "
    "Please continue what you were working on."
)


class ClaudeAuthError(RuntimeError):
    """Raised when an auth flow operation cannot complete."""


class CredentialPasteError(ClaudeAuthError):
    """Raised when a pasted credential blob fails strict validation."""


# Public type aliases for dependency injection. Tests pass deterministic
# fakes to `ClaudeAuthService`; production code uses the module defaults.
CommandRunner = Callable[..., Any]
PexpectSpawner = Callable[..., Any]


def _default_command_runner(command: list[str], timeout: float, env: Mapping[str, str] | None = None) -> Any:
    return run_local_command_modern_version(command=command, is_checked=False, timeout=timeout, cwd=None, env=env)


def _default_pexpect_spawner(executable: str, args: list[str], timeout: float) -> Any:
    return pexpect.spawn(executable, args, timeout=timeout, encoding="utf-8")


class AuthMode(str, Enum):
    """The auth mode implied by the managed settings-env keys."""

    SUBSCRIPTION = "subscription"
    IMBUE = "imbue"
    API_KEY = "api_key"
    NONE = "none"


class AuthStatus(FrozenModel):
    """Parsed output of `claude auth status --json`, plus the settings-derived mode.

    `subscription_type` is unset for Console accounts and for setup-token
    (oauth_token) sessions, so the frontend conditionally renders the
    success-state copy. `auth_mode` / `masked_key_suffix` are derived from
    the shared settings.json env block, not from the status subprocess.
    """

    logged_in: bool = Field(description="Whether claude is currently authenticated")
    auth_method: str | None = Field(default=None, description="e.g. 'oauth', 'api_key', 'oauth_token'")
    api_provider: str | None = Field(default=None, description="e.g. 'anthropic', 'claudeai', 'firstParty'")
    email: str | None = Field(default=None)
    org_id: str | None = Field(default=None)
    org_name: str | None = Field(default=None)
    subscription_type: str | None = Field(default=None, description="e.g. 'Max'; absent for token/Console sessions")
    auth_mode: AuthMode = Field(default=AuthMode.NONE, description="Mode derived from the managed settings env keys")
    masked_key_suffix: str | None = Field(
        default=None, description="Last few characters of the managed key/token, for display"
    )
    workspace_host_id: str | None = Field(
        default=None, description="This mind's mngr host id, for the desktop app's key-mint page link"
    )


class SetupTokenStartResult(FrozenModel):
    """Result of spawning `claude setup-token`."""

    session_id: str = Field(description="Opaque token for the in-flight setup-token session")
    oauth_url: str = Field(description="URL the user opens to authorize the login")


class SetupTokenPollResult(FrozenModel):
    """Result of polling an in-flight setup-token session."""

    is_complete: bool = Field(description="Whether the token was minted and written")
    status: AuthStatus | None = Field(default=None, description="Auth status after completion; None while pending")


class _SetupTokenSessionRecord(FrozenModel):
    """Immutable handle for an in-flight setup-token subprocess.

    Pairs with a parallel non-frozen slot that holds the live pexpect
    process object, since that object is not Pydantic-serializable.
    """

    session_id: str
    oauth_url: str


class AgentSnapshot(FrozenModel):
    """One claude-binary agent's name and lifecycle state at snapshot time."""

    name: str = Field(description="Agent name (used to address mngr stop/start/message)")
    state: str = Field(description="Lifecycle state string from mngr list (e.g. 'RUNNING', 'WAITING')")


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


@pure
def parse_credential_lines(pasted_text: str) -> dict[str, str]:
    """Parse a pasted env-var-style credential blob into the managed keys.

    Strict by design: the settings env block is fully controlled, so a paste
    is rejected (rather than partially applied) when it contains any key
    outside the managed set, mixes an OAuth token with an API key (the key
    would silently outrank the token at runtime), supplies a base URL with
    no key, or contains no managed key at all.

    Raises CredentialPasteError with a user-facing message on any violation.
    """
    parsed = parse_env_file(pasted_text)
    stripped = {key: value.strip() for key, value in parsed.items() if value.strip()}
    if not stripped:
        raise CredentialPasteError("No credentials found. Paste lines like ANTHROPIC_API_KEY=sk-ant-...")
    unknown_keys = sorted(set(stripped) - MANAGED_AUTH_ENV_KEYS)
    if unknown_keys:
        raise CredentialPasteError(
            "Unsupported keys in paste: {}. Only {} are accepted.".format(
                ", ".join(unknown_keys), ", ".join(sorted(MANAGED_AUTH_ENV_KEYS))
            )
        )
    has_token = CLAUDE_CODE_OAUTH_TOKEN_ENV_VAR in stripped
    has_key = ANTHROPIC_API_KEY_ENV_VAR in stripped
    has_base_url = ANTHROPIC_BASE_URL_ENV_VAR in stripped
    if has_token and (has_key or has_base_url):
        raise CredentialPasteError(
            "Paste either an OAuth token OR an API key (with optional base URL), not both: "
            "an API key would silently take precedence over the token."
        )
    if has_base_url and not has_key:
        raise CredentialPasteError(
            f"{ANTHROPIC_BASE_URL_ENV_VAR} requires an accompanying {ANTHROPIC_API_KEY_ENV_VAR}."
        )
    return stripped


@pure
def derive_auth_mode(managed_env: Mapping[str, str]) -> AuthMode:
    """Derive the auth mode implied by the managed settings-env keys.

    Mirrors Claude Code's credential precedence: an API key outranks an
    OAuth token, and a key paired with a base URL means requests route to
    a proxy (the Imbue LiteLLM case).
    """
    if managed_env.get(ANTHROPIC_API_KEY_ENV_VAR):
        if managed_env.get(ANTHROPIC_BASE_URL_ENV_VAR):
            return AuthMode.IMBUE
        return AuthMode.API_KEY
    elif managed_env.get(CLAUDE_CODE_OAUTH_TOKEN_ENV_VAR):
        return AuthMode.SUBSCRIPTION
    else:
        return AuthMode.NONE


@pure
def masked_credential_suffix(managed_env: Mapping[str, str]) -> str | None:
    """Last few characters of the active managed credential, for display."""
    credential = managed_env.get(ANTHROPIC_API_KEY_ENV_VAR) or managed_env.get(CLAUDE_CODE_OAUTH_TOKEN_ENV_VAR)
    if not credential:
        return None
    return credential[-_DISPLAY_SUFFIX_LENGTH:]


def read_workspace_host_id() -> str | None:
    """Read this mind's mngr host id from `$MNGR_HOST_DIR/data.json`.

    Tolerant: returns None when the env var or file is missing/corrupt --
    the host id only powers the desktop app's key-mint page link, and the
    rest of the modal must keep working without it.
    """
    host_dir = os.environ.get(_HOST_DIR_ENV_VAR, "")
    if not host_dir:
        return None
    data_path = Path(host_dir) / "data.json"
    if not data_path.exists():
        return None
    try:
        data = json.loads(data_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Cannot read host data.json at {}: {}", data_path, e)
        return None
    host_id = data.get("host_id") if isinstance(data, dict) else None
    return host_id if isinstance(host_id, str) and host_id else None


def _resolve_claude_config_dir() -> Path:
    config_dir = os.environ.get(_CLAUDE_CONFIG_DIR_ENV_VAR, "")
    if not config_dir:
        raise ClaudeAuthError(f"{_CLAUDE_CONFIG_DIR_ENV_VAR} is unset; cannot locate the Claude config")
    return Path(config_dir)


def _resolve_claude_settings_path() -> Path:
    """Locate the shared `$CLAUDE_CONFIG_DIR/settings.json` for the mind."""
    return _resolve_claude_config_dir() / "settings.json"


def _resolve_claude_config_path() -> Path:
    """Locate the shared `$CLAUDE_CONFIG_DIR/.claude.json` for the mind."""
    return _resolve_claude_config_dir() / ".claude.json"


def read_managed_auth_env(settings_path_override: Path | None = None) -> dict[str, str]:
    """Read the managed auth keys currently in the shared settings.json env block."""
    settings_path = settings_path_override or _resolve_claude_settings_path()
    if not settings_path.exists():
        return {}
    try:
        settings = json.loads(settings_path.read_text())
    except json.JSONDecodeError as e:
        logger.warning("Corrupt settings.json at {}: {}", settings_path, e)
        return {}
    if not isinstance(settings, dict):
        logger.warning("Non-object settings.json at {}", settings_path)
        return {}
    env = settings.get("env")
    if not isinstance(env, dict):
        return {}
    return {key: str(value) for key, value in env.items() if key in MANAGED_AUTH_ENV_KEYS and isinstance(value, str)}


def write_managed_auth_env(managed_env: Mapping[str, str], settings_path_override: Path | None = None) -> Path:
    """Write the managed auth keys into the shared settings.json env block.

    Fully controlled: every managed key absent from `managed_env` is DELETED
    from the env block, so a mode switch can never leave a stale credential
    behind to shadow the new one. Non-managed env keys and every other
    setting are preserved untouched.
    """
    for key in managed_env:
        if key not in MANAGED_AUTH_ENV_KEYS:
            raise ClaudeAuthError(f"Refusing to write unmanaged settings env key {key!r}")
    settings_path = settings_path_override or _resolve_claude_settings_path()
    settings: dict[str, Any] = {}
    if settings_path.exists():
        try:
            loaded = json.loads(settings_path.read_text())
        except json.JSONDecodeError as e:
            # A corrupt shared settings file would break every claude in the
            # mind well beyond auth; refuse to silently replace it.
            raise ClaudeAuthError(f"Shared Claude settings at {settings_path} are corrupt JSON: {e}") from e
        if not isinstance(loaded, dict):
            raise ClaudeAuthError(f"Shared Claude settings at {settings_path} are not a JSON object")
        settings = loaded
    env = settings.get("env")
    if not isinstance(env, dict):
        env = {}
    preserved = {key: value for key, value in env.items() if key not in MANAGED_AUTH_ENV_KEYS}
    updated_env = {**preserved, **dict(managed_env)}
    if updated_env:
        settings["env"] = updated_env
    else:
        settings.pop("env", None)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    logger.info("Wrote managed auth env ({} mode) to {}", derive_auth_mode(managed_env).value, settings_path)
    return settings_path


def _approve_api_key_in_claude_config(config_path: Path, api_key: SecretStr) -> None:
    """Add `api_key` to `customApiKeyResponses.approved` in the Claude config.

    Claude Code challenges any `ANTHROPIC_API_KEY` it finds in the
    environment (including one injected via the settings env block) that
    isn't pre-approved, via an interactive TUI prompt that a restarted
    agent would then block on. Approvals are keyed by the last 20
    characters of the key (mirrors mngr_claude's
    `approve_api_key_for_claude`). This runs while every agent is stopped,
    so a plain read/write is safe -- no concurrent writer to race.
    """
    config = read_claude_config(config_path)
    responses = config.get("customApiKeyResponses")
    if not isinstance(responses, dict):
        responses = {}
    approved = list(responses.get("approved", []))
    suffix = api_key.get_secret_value()[-_API_KEY_APPROVAL_SUFFIX_LENGTH:]
    if suffix not in approved:
        approved.append(suffix)
    responses["approved"] = approved
    responses.setdefault("rejected", [])
    config["customApiKeyResponses"] = responses
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2) + "\n")


def _prepare_claude_config_for_restart(api_key: SecretStr | None) -> None:
    """Pre-dismiss Claude Code's startup dialogs before agents restart.

    A freshly restarted agent re-runs Claude Code's first-launch flow
    (theme picker, onboarding, custom-API-key challenge). Any of those is
    an interactive TUI prompt that the agent would block on. mngr's claude
    plugin dismisses them at agent-creation time; the modal's restart
    paths must do the same so the recovered agent comes up usable.

    Called between stopping and starting the agents, so the running agents
    cannot clobber the file from their stale in-memory copy.
    """
    config_path = _resolve_claude_config_path()
    complete_onboarding(config_path)
    dismiss_effort_callout(config_path)
    acknowledge_cost_threshold(config_path)
    if api_key is not None:
        _approve_api_key_in_claude_config(config_path, api_key)


def _safe_terminate(process: Any) -> None:
    """Terminate a pexpect spawn without letting teardown errors propagate.

    `pexpect.spawn.isalive()` reaps the child's exit status and wraps
    `ptyprocess` errors in `pexpect.ExceptionPexpect`; `terminate()` can
    raise `OSError` on an already-reaped descriptor. Both live inside the
    try so a half-torn-down process never crashes the caller (called from
    every setup-token teardown path, including the auth-success chokepoint).
    """
    try:
        if not process.isalive():
            return
        process.terminate(force=True)
    except (OSError, pexpect.ExceptionPexpect) as e:
        logger.warning("setup-token subprocess terminate raised: {}", e)


def _safe_close(process: Any) -> None:
    """Release the pexpect spawn's PTY file descriptor.

    `pexpect.spawn.close()` can raise `OSError` (e.g. on an already-closed
    descriptor) and `pexpect.ExceptionPexpect` in some teardown paths.
    Swallow + log both since the only thing we can do at this point is
    drop the reference anyway.
    """
    try:
        process.close()
    except (OSError, pexpect.ExceptionPexpect) as e:
        logger.warning("setup-token subprocess close raised: {}", e)


@pure
def _split_into_render_fragments(raw_output: str) -> list[str]:
    """Split raw PTY output into visible-text fragments.

    Escape sequences and control bytes become fragment boundaries (rather
    than being deleted, which would glue adjacent fragments into one bogus
    run of text -- the CLI's Ink renderer emits diff-based frames full of
    cursor-positioning escapes between fragments).
    """
    return _TERMINAL_ESCAPE_OR_CONTROL_REGEX.sub("\n", raw_output).split("\n")


@pure
def _join_wrapped_fragments(
    fragments: list[str],
    start_idx: int,
    start_col: int,
    continuation_regex: re.Pattern[str],
) -> tuple[str, bool]:
    """Re-assemble a value hard-wrapped across terminal rows.

    A wrapped row runs to the full terminal width, so every row of the value
    except the last is as wide as the widest fragment in the capture. Join
    the following fragment (leading indentation stripped) only while the
    current row ran full-width and the next fragment is charset-pure.

    Also reports whether the value provably *ended*: its last row stopped
    short of full width, or the following fragment broke the charset. A
    full-width row with nothing after it may still be mid-stream.
    """
    width = max(len(fragment) for fragment in fragments)
    value = fragments[start_idx][start_col:]
    idx = start_idx
    while len(fragments[idx]) == width:
        # Line endings and escape sequences leave empty fragments between
        # rendered rows; skip them to find the actual next row.
        next_idx = idx + 1
        while next_idx < len(fragments) and fragments[next_idx] == "":
            next_idx += 1
        if next_idx >= len(fragments):
            return value, False
        candidate = fragments[next_idx].lstrip()
        if continuation_regex.match(candidate) is None:
            return value, True
        value += candidate
        idx = next_idx
    return value, True


@pure
def _extract_wrapped_value(
    raw_output: str,
    start_regex: re.Pattern[str],
    continuation_regex: re.Pattern[str],
    is_termination_required: bool,
) -> str | None:
    """Find `start_regex` in the visible PTY text, de-wrapping across rows.

    With `is_termination_required`, a value whose end is not yet provable
    (the stream may still be mid-value) yields None so the caller keeps
    draining.
    """
    fragments = _split_into_render_fragments(raw_output)
    for idx, fragment in enumerate(fragments):
        match = start_regex.search(fragment)
        if match is not None:
            value, is_terminated = _join_wrapped_fragments(fragments, idx, match.start(), continuation_regex)
            if is_termination_required and not is_terminated:
                return None
            return value
    return None


@pure
def _extract_oauth_url_from_hyperlink(raw_output: str) -> str | None:
    """Pull the OAuth URL from an OSC 8 hyperlink target in the raw stream.

    The CLI renders the URL as an OSC 8 terminal hyperlink; the (invisible)
    target carries the full URL with no width-wrapping, unlike the visible
    label, which Ink hard-wraps at the terminal width. Only *terminated*
    sequences match, so a half-received target is never returned.
    """
    for match in _OSC8_HYPERLINK_REGEX.finditer(raw_output):
        target_match = _OAUTH_URL_STRICT_REGEX.search(match.group(1))
        if target_match is not None:
            return target_match.group(0)
    return None


@pure
def _extract_oauth_url(raw_output: str) -> str | None:
    """Pull the single OAuth URL out of `claude setup-token`'s PTY output.

    Prefers the OSC 8 hyperlink target (complete by construction); falls
    back to re-assembling the width-wrapped visible label when the CLI did
    not emit a hyperlink.
    """
    from_hyperlink = _extract_oauth_url_from_hyperlink(raw_output)
    if from_hyperlink is not None:
        return from_hyperlink
    return _extract_wrapped_value(
        raw_output, _OAUTH_URL_STRICT_REGEX, _OAUTH_URL_CONTINUATION_REGEX, is_termination_required=False
    )


@pure
def _extract_setup_token(raw_output: str, is_termination_required: bool) -> str | None:
    """Pull the minted `sk-ant-oat01-...` token out of the PTY output.

    The token is longer than an 80-column row, so it may be width-wrapped
    just like the OAuth URL (but has no hyperlink copy). A too-short
    extraction is a wrapped fragment, not the token -- return None so the
    caller keeps draining instead of storing a truncated token.
    """
    token = _extract_wrapped_value(
        raw_output, _SETUP_TOKEN_STRICT_REGEX, _SETUP_TOKEN_CONTINUATION_REGEX, is_termination_required
    )
    if token is None or len(token) < _MIN_SETUP_TOKEN_LENGTH:
        return None
    return token


def _drain_pty_stream(process: Any, consumed: str, is_complete: Callable[[str], bool]) -> str:
    """Keep reading PTY output until `is_complete(consumed)` or a deadline.

    `process.expect` returns as soon as its trigger pattern matches, which
    can be mid-escape-sequence or mid-render-frame, so the buffer may hold
    only a prefix of the value being extracted. The CLI animates its spinner
    indefinitely, so there is no reliable quiet gap; completion is judged by
    the caller's predicate, with a hard deadline as backstop.
    """
    deadline = time.monotonic() + _STREAM_DRAIN_DEADLINE_SECONDS
    while not is_complete(consumed) and time.monotonic() < deadline:
        try:
            chunk = process.read_nonblocking(size=65536, timeout=_STREAM_DRAIN_READ_SECONDS)
        except pexpect.TIMEOUT:
            continue
        except pexpect.EOF:
            break
        consumed = consumed + (chunk or "")
    return consumed


def _build_list_command() -> list[str]:
    """Build the ``mngr list`` argv used to enumerate agents.

    Pure: argv assembly only, so the repo<->mngr CLI contract is testable
    against the live CLI without a subprocess (see ``claude_auth_test.py``).

    ``--on-error continue`` makes this blanket listing tolerate an
    unauthenticated/unreachable provider: ``mngr list`` still emits the
    healthy providers' agents and exits ``EXIT_CODE_PROVIDER_INACCESSIBLE``,
    which the caller treats as success.
    """
    return ["mngr", "list", "--format", "json", "--on-error", "continue"]


def _log_inaccessible_providers(payload: dict[str, Any]) -> None:
    """Debug-log each provider `mngr list` skipped due to an auth/access error.

    The structured `errors` array is present when `mngr list` exits
    EXIT_CODE_PROVIDER_INACCESSIBLE. Skipped providers are expected (e.g. a
    provider enabled in config but never authenticated), so this is debug
    only -- the enumeration still succeeds on the healthy providers.
    """
    errors = payload.get("errors", [])
    if not isinstance(errors, list):
        return
    for error in errors:
        if not isinstance(error, dict):
            continue
        provider_name = error.get("provider_name", "?")
        message = error.get("message", "")
        logger.debug("Skipped inaccessible provider {} while listing agents: {}", provider_name, message)


def _build_stop_command(name: str) -> list[str]:
    """Build the ``mngr stop`` argv for one agent. Pure (see above)."""
    return ["mngr", "stop", name]


def _build_start_command(name: str) -> list[str]:
    """Build the ``mngr start --no-resume`` argv for one agent. Pure (see above)."""
    return ["mngr", "start", "--no-resume", name]


def _build_message_command(name: str, message: str) -> list[str]:
    """Build the ``mngr message`` argv for one agent. Pure (see above)."""
    return ["mngr", "message", name, "-m", message]


class ClaudeAuthService(MutableModel):
    """Stateful entry point for the in-mind Claude auth flows.

    Holds the injected `command_runner` / `pexpect_spawner` dependencies
    and the in-flight setup-token subprocess. One instance is created per
    application and stored on `app.state`; the subprocess held between
    `start_setup_token` and its poll/submit calls rides that instance.
    Tests construct isolated instances with deterministic fakes.
    """

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    command_runner: CommandRunner = _default_command_runner
    pexpect_spawner: PexpectSpawner = _default_pexpect_spawner

    # Only one setup-token flow can be live at a time per instance, which
    # matches the single-mind / single-user deployment model. The lock and
    # the live subprocess are private runtime state, not configuration data.
    _setup_token_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _current_setup_token_record: _SetupTokenSessionRecord | None = PrivateAttr(default=None)
    _current_setup_token_process: Any = PrivateAttr(default=None)
    _current_setup_token_output: str = PrivateAttr(default="")

    def get_auth_status(self, extra_env: Mapping[str, str] | None = None) -> AuthStatus:
        """Invoke `claude auth status --json` and parse the result.

        Returns `logged_in=False` if the `claude` binary is missing or
        doesn't produce output, rather than raising, since the whole point
        of the modal is to recover from broken auth state.

        The managed env currently in settings.json is overlaid on the
        status subprocess's environment (with `extra_env` layered on top):
        the settings env applies to *new claude processes*, and the status
        subprocess IS one, but the fresh values may not have reached this
        long-lived system-interface process -- the overlay makes the check
        reflect the mind's actual auth source of truth. The settings-derived
        `auth_mode` / `masked_key_suffix` are folded into the returned
        status for the modal's header.
        """
        managed_env = self._read_managed_env_tolerant()
        combined_extra = {**managed_env, **(dict(extra_env) if extra_env else {})}
        runner_env = {**os.environ, **combined_extra} if combined_extra else None
        try:
            result = (
                self.command_runner(
                    ["claude", "auth", "status", "--json"],
                    _CLAUDE_AUTH_STATUS_TIMEOUT_SECONDS,
                    runner_env,
                )
                if runner_env is not None
                else self.command_runner(["claude", "auth", "status", "--json"], _CLAUDE_AUTH_STATUS_TIMEOUT_SECONDS)
            )
        except ProcessSetupError as e:
            logger.warning("claude auth status failed to launch: {}", e)
            return self._with_derived_mode(AuthStatus(logged_in=False), managed_env)

        stdout = result.stdout.strip() if isinstance(result.stdout, str) else ""
        if not stdout:
            return self._with_derived_mode(AuthStatus(logged_in=False), managed_env)
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as e:
            raise ClaudeAuthError(f"claude auth status returned non-JSON output: {stdout!r}") from e
        if not isinstance(payload, dict):
            raise ClaudeAuthError(f"claude auth status returned non-object JSON: {payload!r}")
        return self._with_derived_mode(_parse_status_payload(payload), managed_env)

    def _read_managed_env_tolerant(self) -> dict[str, str]:
        """Read the managed settings env, tolerating an unset CLAUDE_CONFIG_DIR.

        Status checks must not explode merely because the env var is
        missing (e.g. in a degraded mind) -- they degrade to "no managed
        credentials" and the modal walks the user through recovery.
        """
        try:
            return read_managed_auth_env()
        except ClaudeAuthError as e:
            logger.warning("Cannot read managed auth env: {}", e)
            return {}

    @staticmethod
    def _with_derived_mode(status: AuthStatus, managed_env: Mapping[str, str]) -> AuthStatus:
        return AuthStatus(
            **{
                **status.model_dump(),
                "auth_mode": derive_auth_mode(managed_env),
                "masked_key_suffix": masked_credential_suffix(managed_env),
                "workspace_host_id": read_workspace_host_id(),
            }
        )

    def snapshot_claude_binary_agents(self) -> list[AgentSnapshot]:
        """Return name + state of every claude-binary agent in the local mind.

        Uses `mngr list --format json` and filters to the claude-binary
        types (``claude`` and ``worker``). This excludes the `main`-type
        system-services agent, which has no interactive claude process to
        restart -- and whose restart would tear down every background
        service in the mind.
        """
        result = self.command_runner(_build_list_command(), _MNGR_COMMAND_TIMEOUT_SECONDS)
        # Exit EXIT_CODE_PROVIDER_INACCESSIBLE means some enabled provider was
        # unauthenticated/unreachable, but the healthy providers' agents were
        # still listed (we pass --on-error continue). This is a blanket listing,
        # so that is an acceptable partial success: enumerate what we got. Any
        # other nonzero exit is a real failure.
        if result.returncode not in (0, EXIT_CODE_PROVIDER_INACCESSIBLE):
            raise ClaudeAuthError(f"mngr list failed (exit {result.returncode}): {result.stderr.strip()}")
        stdout = result.stdout if isinstance(result.stdout, str) else ""
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as e:
            raise ClaudeAuthError(f"mngr list returned non-JSON output: {stdout!r}") from e
        if not isinstance(payload, dict):
            raise ClaudeAuthError(f"mngr list returned non-object JSON: {payload!r}")
        if result.returncode == EXIT_CODE_PROVIDER_INACCESSIBLE:
            _log_inaccessible_providers(payload)
        agents = payload.get("agents", [])
        if not isinstance(agents, list):
            raise ClaudeAuthError(f"mngr list 'agents' field is not a list: {agents!r}")
        snapshots: list[AgentSnapshot] = []
        for agent in agents:
            if not isinstance(agent, dict):
                continue
            if agent.get("type") not in CLAUDE_BINARY_AGENT_TYPES:
                continue
            name = agent.get("name")
            if not (isinstance(name, str) and name):
                continue
            state = agent.get("state")
            snapshots.append(AgentSnapshot(name=name, state=state if isinstance(state, str) else ""))
        return snapshots

    def restart_all_claude_agents(self, api_key: SecretStr | None = None) -> list[str]:
        """Restart every live claude-binary agent via `mngr stop` then `mngr start`.

        Snapshots agent states first, then stops every live (RUNNING or
        WAITING) agent, prepares the shared Claude config (see
        `_prepare_claude_config_for_restart`), starts them again, and
        finally messages the agents that were RUNNING so interrupted work
        resumes. STOPPED agents are left stopped. The
        stop-all/prepare/start-all ordering matters: editing `.claude.json`
        while an agent is still running would be silently overwritten by
        that agent's stale in-memory copy on its next write.

        Agents are started with `--no-resume` so mngr does not deliver the
        configured resume message after the restart; the previously-RUNNING
        agents instead get the explicit auth-aware continue message, which
        explains the interruption rather than pretending it did not happen.

        `api_key`, when given, is additionally approved in the Claude
        config so a freshly-written key doesn't trip Claude's custom-key
        challenge.

        Returns the list of agent names that were restarted.
        """
        snapshots = self.snapshot_claude_binary_agents()
        live_agents = [s for s in snapshots if s.state in (_AGENT_STATE_RUNNING, _AGENT_STATE_WAITING)]
        for snapshot in live_agents:
            logger.info("Stopping claude-binary agent {} via mngr stop", snapshot.name)
            stop_result = self.command_runner(_build_stop_command(snapshot.name), _MNGR_COMMAND_TIMEOUT_SECONDS)
            if stop_result.returncode != 0:
                raise ClaudeAuthError(
                    f"mngr stop {snapshot.name} failed (exit {stop_result.returncode}): {stop_result.stderr.strip()}"
                )
        _prepare_claude_config_for_restart(api_key)
        for snapshot in live_agents:
            logger.info("Starting claude-binary agent {} via mngr start --no-resume", snapshot.name)
            start_result = self.command_runner(_build_start_command(snapshot.name), _MNGR_COMMAND_TIMEOUT_SECONDS)
            if start_result.returncode != 0:
                raise ClaudeAuthError(
                    f"mngr start {snapshot.name} failed (exit {start_result.returncode}): {start_result.stderr.strip()}"
                )
        # Message the agents that were mid-task so their work resumes. A
        # delivery failure must not fail the whole auth flow (auth itself
        # succeeded); it is logged so the user can nudge the agent manually.
        for snapshot in live_agents:
            if snapshot.state != _AGENT_STATE_RUNNING:
                continue
            logger.info("Messaging previously-RUNNING agent {} to continue after auth restart", snapshot.name)
            message_result = self.command_runner(
                _build_message_command(snapshot.name, RESTART_CONTINUE_MESSAGE), _MNGR_MESSAGE_TIMEOUT_SECONDS
            )
            if message_result.returncode != 0:
                logger.warning(
                    "Failed to deliver continue message to agent {} (exit {}): {}",
                    snapshot.name,
                    message_result.returncode,
                    message_result.stderr.strip() if isinstance(message_result.stderr, str) else "",
                )
        return [s.name for s in live_agents]

    def submit_credentials(self, pasted_text: str) -> AuthStatus:
        """Parse pasted credentials, write the settings env block, restart agents.

        The single chokepoint for the API-key field, the Imbue blob
        textarea, and the subtle direct-token paste: all three arrive as
        env-var-style lines and land in the fully-controlled settings env
        block. All claude-binary agents must be restarted: settings env is
        read at process start, so already-running claudes won't pick up the
        new credentials until their tmux sessions are torn down and
        respawned.
        """
        managed_env = parse_credential_lines(pasted_text)
        write_managed_auth_env(managed_env)
        api_key_value = managed_env.get(ANTHROPIC_API_KEY_ENV_VAR)
        self.restart_all_claude_agents(api_key=SecretStr(api_key_value) if api_key_value else None)
        return self.get_auth_status(extra_env=managed_env)

    def _spawn_setup_token_and_parse_url(self) -> tuple[Any, str, str]:
        process = self.pexpect_spawner(
            "claude",
            ["setup-token"],
            _OAUTH_URL_WAIT_SECONDS,
        )
        match_index = process.expect([_OAUTH_URL_REGEX, pexpect.EOF, pexpect.TIMEOUT])
        if match_index != 0:
            _safe_terminate(process)
            _safe_close(process)
            if match_index == 1:
                raise ClaudeAuthError("claude setup-token exited before printing the OAuth URL")
            raise ClaudeAuthError("Timed out waiting for the OAuth URL from claude setup-token")
        # The expect trigger can fire mid-render-frame -- e.g. inside the OSC 8
        # hyperlink's opening sequence or on the first width-wrapped row of the
        # visible label -- so the consumed buffer may hold only a prefix of the
        # URL. Drain until a *terminated* hyperlink target is extractable (the
        # normal case, satisfied within the same frame); if the CLI emitted no
        # hyperlink, the deadline expires and the visible label is de-wrapped
        # from everything drained.
        initial_consumed = (process.before or "") + (process.after or "")
        consumed = _drain_pty_stream(
            process,
            initial_consumed,
            lambda buffer: _extract_oauth_url_from_hyperlink(buffer) is not None,
        )
        oauth_url = _extract_oauth_url(consumed)
        if oauth_url is None:
            _safe_terminate(process)
            _safe_close(process)
            raise ClaudeAuthError(
                "OAuth URL matched in the stream but could not be extracted after stripping terminal escape sequences"
            )
        return process, oauth_url, consumed

    def start_setup_token(self) -> SetupTokenStartResult:
        """Spawn `claude setup-token` and return the parsed OAuth URL.

        Replaces any prior in-flight session: only one setup-token flow can
        be live at a time per instance, which matches the single-mind /
        single-user deployment model. The subprocess then polls Anthropic
        on its own; the frontend drives `poll_setup_token` until the token
        appears (or pastes a code via `submit_setup_token_code` if the CLI
        demands one).
        """
        with self._setup_token_lock:
            self._drop_current_session_locked()
            process, oauth_url, consumed = self._spawn_setup_token_and_parse_url()
            record = _SetupTokenSessionRecord(session_id=uuid.uuid4().hex, oauth_url=oauth_url)
            self._current_setup_token_record = record
            self._current_setup_token_process = process
            self._current_setup_token_output = consumed
        return SetupTokenStartResult(session_id=record.session_id, oauth_url=record.oauth_url)

    def _drop_current_session_locked(self) -> None:
        if self._current_setup_token_process is not None:
            _safe_terminate(self._current_setup_token_process)
            _safe_close(self._current_setup_token_process)
        self._current_setup_token_record = None
        self._current_setup_token_process = None
        self._current_setup_token_output = ""

    def _pump_setup_token_output_locked(self, timeout_seconds: float) -> str | None:
        """Read newly available subprocess output; return the token if it appeared.

        Uses a short expect against the token pattern so each poll returns
        promptly. On EOF the accumulated buffer is scanned once more (the
        token and process exit can arrive together); an EOF without a token
        anywhere in the output means the subprocess failed.
        """
        process = self._current_setup_token_process
        try:
            match_index = process.expect(
                [_SETUP_TOKEN_REGEX, _OAUTH_ERROR_REGEX, pexpect.EOF, pexpect.TIMEOUT], timeout=timeout_seconds
            )
        except pexpect.ExceptionPexpect as e:
            raise ClaudeAuthError(f"claude setup-token subprocess failed while waiting for the token: {e}") from e
        self._current_setup_token_output += (process.before or "") + (
            process.after if isinstance(process.after, str) else ""
        )
        if match_index == 1:
            raise ClaudeAuthError(
                "Sign-in was not accepted (OAuth error). The pasted code may be wrong, expired, "
                "or from an earlier sign-in attempt. Please start over."
            )
        if match_index == 0:
            # The trigger fires on the first (possibly width-wrapped) token
            # fragment; the CLI prints the token as its final output and
            # exits, so drain the remainder before extracting. During the
            # drain a token only counts once its end is provable; the final
            # extraction below takes the best available value (the drain
            # ends at EOF or the deadline, so the stream is as complete as
            # it is going to get).
            self._current_setup_token_output = _drain_pty_stream(
                process,
                self._current_setup_token_output,
                lambda buffer: _extract_setup_token(buffer, is_termination_required=True) is not None,
            )
        token = _extract_setup_token(self._current_setup_token_output, is_termination_required=False)
        if token is not None:
            return token
        if match_index == 2:
            raise ClaudeAuthError("claude setup-token exited without printing a token")
        return None

    def _complete_setup_token_locked(self, token: str) -> AuthStatus:
        """Write the minted token to settings env, restart agents, drop the session."""
        self._drop_current_session_locked()
        managed_env = {CLAUDE_CODE_OAUTH_TOKEN_ENV_VAR: token}
        write_managed_auth_env(managed_env)
        self.restart_all_claude_agents(api_key=None)
        return self.get_auth_status(extra_env=managed_env)

    def poll_setup_token(self, session_id: str) -> SetupTokenPollResult:
        """Check whether the in-flight setup-token subprocess minted the token yet.

        The browser approval completes the flow CLI-side without any code
        paste (the CLI polls Anthropic), so the frontend just calls this
        periodically. On completion the token is written to the settings
        env block and the claude agents are restarted before returning.
        """
        with self._setup_token_lock:
            record = self._current_setup_token_record
            if record is None or record.session_id != session_id:
                raise ClaudeAuthError("No active setup-token session matches the provided session_id")
            try:
                token = self._pump_setup_token_output_locked(_SETUP_TOKEN_POLL_SECONDS)
            except ClaudeAuthError:
                self._drop_current_session_locked()
                raise
            if token is None:
                return SetupTokenPollResult(is_complete=False)
            status = self._complete_setup_token_locked(token)
        return SetupTokenPollResult(is_complete=True, status=status)

    def submit_setup_token_code(self, session_id: str, code: str) -> AuthStatus:
        """Send the user's pasted `CODE#STATE` to the live setup-token subprocess.

        The fallback path for flows where the CLI actually prompts for a
        code paste instead of completing via its own polling.
        """
        with self._setup_token_lock:
            record = self._current_setup_token_record
            process = self._current_setup_token_process
            if record is None or process is None or record.session_id != session_id:
                raise ClaudeAuthError("No active setup-token session matches the provided session_id")
            try:
                # Two separate writes: the CLI's paste heuristic swallows a
                # newline arriving in the same burst as the code (it becomes
                # field content, not a submit), so Enter goes as its own
                # deferred keystroke.
                process.send(code)
                time.sleep(_SETUP_TOKEN_CODE_ENTER_DELAY_SECONDS)
                process.send("\r")
            except pexpect.ExceptionPexpect as e:
                self._drop_current_session_locked()
                raise ClaudeAuthError(f"claude setup-token subprocess failed sending code: {e}") from e
            try:
                token = self._pump_setup_token_output_locked(_SETUP_TOKEN_CODE_WAIT_SECONDS)
            except ClaudeAuthError:
                self._drop_current_session_locked()
                raise
            if token is None:
                self._drop_current_session_locked()
                raise ClaudeAuthError("Timed out waiting for claude setup-token to print the token after code submit")
            status = self._complete_setup_token_locked(token)
        return status

    def abort_setup_token(self) -> None:
        """Drop any in-flight setup-token session (e.g. user closed the modal)."""
        with self._setup_token_lock:
            self._drop_current_session_locked()
