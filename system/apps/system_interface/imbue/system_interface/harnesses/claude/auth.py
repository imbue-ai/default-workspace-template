"""What is left of the claude login modal's backend: reading and parsing claude's auth state.

Sign-in moved to the provider chooser, which is harness-agnostic and writes into a
per-account folder (`accounts.py`, `harnesses/auth_flows.py`). The pieces that made this
module the *writer* of the workspace's one shared credential went with it:

* `write_managed_auth_env` -- the shared `settings.json` env block. An account's own
  settings.json is written by `auth_flows.write_claude_env` now.
* `restart_all_claude_agents` and its snapshot/resume machinery -- a shared credential had
  to be pushed into every running agent, since claude reads settings-env at process start.
  An account is chosen at create time instead, so nothing needs restarting.
* the setup-token PTY flow -- `harnesses/pty_auth.py` drives every harness's terminal
  sign-in now, and `lanes.py` says which patterns each one needs.

What remains is the read side, still used by two callers:

* `get_auth_status` backs `GET /api/claude-auth/status`, which mngr's own deployment test
  drives, and answers "what is this claude authenticated as" for a given environment.
* `parse_credential_lines` / `MANAGED_AUTH_ENV_KEYS` / `record_api_key_approval` are the
  vocabulary of a claude credential -- what the three managed keys are, what a pasted
  env-lines blob may contain, and how to stop claude challenging a key it has not seen.
  The chooser's paste lane uses all three.
"""

from __future__ import annotations


import json
import os
import re
import time
from collections.abc import Callable
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import Any
from typing import Final

import pexpect
from loguru import logger as _loguru_logger
from pydantic import Field

from imbue.concurrency_group.subprocess_utils import ProcessSetupError
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mngr.utils.env_utils import parse_env_file
from imbue.mngr_claude.claude_config import find_user_config_in_unisolated_mode
from imbue.mngr_claude.claude_config import get_claude_config_dir
from imbue.system_interface.harnesses.pty_auth import PtyAuthError
from imbue.system_interface.harnesses.pty_auth import extract_hyperlink_value
from imbue.system_interface.harnesses.pty_auth import extract_wrapped_value

logger = _loguru_logger

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
# Characters of the key/token shown in the modal's "currently signed in via"
# header; long enough to disambiguate, short enough to stay a non-secret.
_DISPLAY_SUFFIX_LENGTH: Final = 4
# Claude Code identifies an approved ANTHROPIC_API_KEY by its last 20
# characters in `.claude.json`'s `customApiKeyResponses.approved` (the same
# suffix length mngr's `approve_api_key_for_claude` records).
_API_KEY_APPROVAL_SUFFIX_LENGTH: Final = 20
# Fires on the first sight of the OAuth URL in the PTY stream. This is only a
# *trigger*: the CLI's Ink renderer hard-wraps the visible URL at the terminal
# width (pexpect's default PTY is 80 columns) and pexpect can match mid
# render-frame, so the buffer may hold just a prefix. The actual URL is
# recovered by `_extract_oauth_url` after draining the stream.
# Strict charset for re-assembling a width-wrapped URL from visible text:
# unlike `\S`, it excludes stray control bytes left between render fragments.
_OAUTH_URL_CHARSET = r"[A-Za-z0-9%&=?_.~/:+#-]"
_OAUTH_URL_STRICT_REGEX = re.compile(rf"https://{_OAUTH_URL_CHARSET}*oauth/authorize{_OAUTH_URL_CHARSET}*")
_OAUTH_URL_CONTINUATION_REGEX = re.compile(rf"^{_OAUTH_URL_CHARSET}+$")
# The long-lived token `claude setup-token` prints on completion. Like the
# URL regex, only a trigger -- extraction re-assembles the possibly
# width-wrapped token from the drained stream.
# Printed by the CLI when Anthropic rejects a pasted code (wrong, expired, or
# from an earlier attempt's state) or its own polling hits an error; the CLI
# then parks on a "Press Enter to retry." prompt, so without failing fast the
# session would just time out with a misleading message.
# Printed plainly (outside the Ink renderer) by `claude auth login` right
# before it exits 0 / 1 respectively, so no screen replay is needed to
# detect completion of the credentials-based browser sign-ins.
# The CLI's Ink input treats a rapid burst of characters as a paste; Enter
# must arrive as its own later keystroke or it lands in the field as
# content. The burst is over once the input echo goes quiet for
# _CODE_ECHO_QUIET_SECONDS (deadline-capped so a silent PTY cannot stall
# the submit).
_CODE_ECHO_QUIET_SECONDS: Final = 0.3
# Real setup tokens are ~110 characters. A much shorter extraction is a
# wrapped fragment, not the token -- keep waiting rather than storing it.
# A fused `mngr start --restart` call stops, starts, readiness-waits, and
# (for previously-RUNNING agents) messages a whole batch of agents. It runs
# on the background restart thread, so the generous ceiling costs nothing
# in the request path.
_CLAUDE_AUTH_STATUS_TIMEOUT_SECONDS: Final = 10.0


# Sent (via `mngr message`) to agents that were RUNNING when the auth-change
class ClaudeAuthError(PtyAuthError):
    """Raised when an auth flow operation cannot complete."""


class CredentialPasteError(ClaudeAuthError):
    """Raised when a pasted credential blob fails strict validation."""


# Public type aliases for dependency injection. Tests pass deterministic
# fakes to `ClaudeAuthService`; production code uses the module defaults.
CommandRunner = Callable[..., Any]


def _default_command_runner(command: list[str], timeout: float, env: Mapping[str, str] | None = None) -> Any:
    return run_local_command_modern_version(command=command, is_checked=False, timeout=timeout, cwd=None, env=env)




class AuthMode(str, Enum):
    """The workspace's effective auth mode.

    Derived from the managed settings-env keys when any are present; with
    an empty managed env, folded from `claude auth status` so the
    credentials-based browser sign-ins (subscription and Console) surface
    correctly.
    """

    SUBSCRIPTION = "subscription"
    CONSOLE = "console"
    IMBUE = "imbue"
    API_KEY = "api_key"
    NONE = "none"








class AuthStatus(FrozenModel):
    """Parsed output of `claude auth status --json`, plus the derived mode.

    On the pinned Claude Code version, both browser sign-ins report
    `authMethod: "claude.ai"` (the Console-stored key resolves through the
    "/login managed key" source); `subscription_type` is present only for
    subscription accounts, which is the discriminator. It is also unset
    for setup-token (`oauth_token`) sessions.
    """

    logged_in: bool = Field(description="Whether claude is currently authenticated")
    auth_method: str | None = Field(
        default=None, description="e.g. 'claude.ai', 'api_key', 'oauth_token', 'api_key_helper', 'none'"
    )
    api_provider: str | None = Field(default=None, description="e.g. 'anthropic', 'claudeai', 'firstParty'")
    email: str | None = Field(default=None)
    org_id: str | None = Field(default=None)
    org_name: str | None = Field(default=None)
    subscription_type: str | None = Field(default=None, description="e.g. 'Max'; absent for token/Console sessions")
    auth_mode: AuthMode = Field(default=AuthMode.NONE, description="The workspace's effective auth mode")
    masked_key_suffix: str | None = Field(
        default=None, description="Last few characters of the managed key/token, for display"
    )
    workspace_id: str | None = Field(
        default=None,
        description=(
            "This workspace's id (its services agent id; the machine's host id as a fallback), "
            "for the desktop app's key-mint page link"
        ),
    )














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


def read_workspace_id() -> str | None:
    """Read this workspace's id -- its services agent's id -- from mngr host state.

    The workspace is identified by the agent carrying the ``is_primary``
    label (its id is stable for the workspace's whole life, across machine
    changes); the machine's host id is the fallback coordinate for hosts
    whose agent state cannot be read (the desktop dual-accepts both).
    Tolerant: returns None when nothing is readable -- the id only powers
    the desktop app's key-mint page link, and the rest of the modal must
    keep working without it.
    """
    host_dir = os.environ.get(_HOST_DIR_ENV_VAR, "")
    if not host_dir:
        return None
    agents_dir = Path(host_dir) / "agents"
    if agents_dir.is_dir():
        for data_path in sorted(agents_dir.glob("*/data.json")):
            try:
                data = json.loads(data_path.read_text())
            except (OSError, json.JSONDecodeError) as e:
                logger.warning("Cannot read agent data.json at {}: {}", data_path, e)
                continue
            if not isinstance(data, dict):
                continue
            labels = data.get("labels")
            if not isinstance(labels, dict) or labels.get("is_primary") != "true":
                continue
            agent_id = data.get("id")
            if isinstance(agent_id, str) and agent_id:
                return agent_id
    return _read_machine_host_id(Path(host_dir))


def _read_machine_host_id(host_dir: Path) -> str | None:
    """The machine's mngr host id from ``data.json`` (the legacy link coordinate)."""
    data_path = host_dir / "data.json"
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
    """Resolve the shared Claude config dir the way claude itself does.

    ``$CLAUDE_CONFIG_DIR`` when set, else ``~/.claude`` (delegated to
    mngr_claude's ``get_claude_config_dir``). In a minds workspace the env
    var is deliberately unset everywhere (no agent or host env exports it),
    so this resolves to the same ``~/.claude`` a bare ``claude`` in a
    workspace terminal uses.
    """
    return get_claude_config_dir()


def _resolve_claude_json_path() -> Path:
    """Locate the global claude config file (``.claude.json``) claude reads.

    Delegates to mngr_claude's ``find_user_config_in_unisolated_mode``, which
    mirrors claude's own resolution: ``$CLAUDE_CONFIG_DIR/.claude.json`` when
    the env var is set, but ``~/.claude.json`` -- BESIDE ``~/.claude/``, not
    inside it -- when unset. Writing the approval into the wrong one of the
    two would leave claude challenging the key.
    """
    return find_user_config_in_unisolated_mode()


def _resolve_claude_settings_path() -> Path:
    """Locate the shared `settings.json` (inside the config dir) for the mind."""
    return _resolve_claude_config_dir() / "settings.json"


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




def record_api_key_approval(managed_env: Mapping[str, str], claude_json_path_override: Path | None = None) -> None:
    """Pre-approve a managed ``ANTHROPIC_API_KEY`` in `.claude.json` so claude never challenges it.

    Interactive claude challenges any ``ANTHROPIC_API_KEY`` it sees in its
    env -- the settings env block included -- whose last-20-character suffix
    is not in ``customApiKeyResponses.approved``: a "Do you want to use this
    API key?" TUI dialog that blocks the agent restart following a
    credential write (and with it the welcome resend, whose message dispatch
    times out waiting for a TUI prompt that never appears). Headless
    ``claude -p`` runs skip the dialog, which is why probes and ``-p``-based
    tests do not reproduce it. mngr's ``approve_api_key_for_claude`` records
    the approval only for keys present at agent-creation time; keys written
    later through the sign-in modal (the API-key and Imbue paths) must be
    approved here, before the restart relaunches the agents. Mirrors that
    helper's format: append the suffix to ``approved``, reset ``rejected``.

    No-op when the managed env carries no API key: an approval for an
    absent key is inert, so stale approvals are deliberately not scrubbed
    on clearing or mode switches.
    """
    api_key = managed_env.get(ANTHROPIC_API_KEY_ENV_VAR, "")
    if not api_key:
        return
    claude_json_path = claude_json_path_override or _resolve_claude_json_path()
    data: dict[str, Any] = {}
    if claude_json_path.exists():
        try:
            loaded = json.loads(claude_json_path.read_text())
        except json.JSONDecodeError as e:
            # A corrupt shared claude config would leave the restarted agents
            # stuck on the interactive challenge; fail the apply loudly
            # instead of restarting into a deadlock.
            raise ClaudeAuthError(f"Shared Claude config at {claude_json_path} is corrupt JSON: {e}") from e
        if not isinstance(loaded, dict):
            raise ClaudeAuthError(f"Shared Claude config at {claude_json_path} is not a JSON object")
        data = loaded
    responses_raw = data.get("customApiKeyResponses")
    responses: dict[str, Any] = responses_raw if isinstance(responses_raw, dict) else {}
    approved_raw = responses.get("approved")
    approved = [entry for entry in approved_raw if isinstance(entry, str)] if isinstance(approved_raw, list) else []
    suffix = api_key[-_API_KEY_APPROVAL_SUFFIX_LENGTH:]
    if suffix not in approved:
        approved.append(suffix)
    responses["approved"] = approved
    responses["rejected"] = []
    data["customApiKeyResponses"] = responses
    claude_json_path.parent.mkdir(parents=True, exist_ok=True)
    claude_json_path.write_text(json.dumps(data, indent=2) + "\n")
    logger.info("Recorded managed API-key approval in {}", claude_json_path)


@pure
def _extract_oauth_url_from_hyperlink(raw_output: str) -> str | None:
    """Pull the OAuth URL from an OSC 8 hyperlink target in the raw stream."""
    return extract_hyperlink_value(raw_output, _OAUTH_URL_STRICT_REGEX)


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
    return extract_wrapped_value(raw_output, _OAUTH_URL_STRICT_REGEX, _OAUTH_URL_CONTINUATION_REGEX)












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
        managed_env = read_managed_auth_env()
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
            return self._with_derived_mode(AuthStatus(logged_in=False), combined_extra)

        stdout = result.stdout.strip() if isinstance(result.stdout, str) else ""
        if not stdout:
            return self._with_derived_mode(AuthStatus(logged_in=False), combined_extra)
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as e:
            raise ClaudeAuthError(f"claude auth status returned non-JSON output: {stdout!r}") from e
        if not isinstance(payload, dict):
            raise ClaudeAuthError(f"claude auth status returned non-object JSON: {payload!r}")
        return self._with_derived_mode(_parse_status_payload(payload), combined_extra)

    def _with_derived_mode(self, status: AuthStatus, managed_env: Mapping[str, str]) -> AuthStatus:
        # Managed env keys outrank everything claude reads elsewhere, so
        # they define the mode when present. With an empty managed env the
        # mode folds in the credentials-based browser sign-ins: both report
        # authMethod "claude.ai" on the pinned version, discriminated by
        # subscription_type (present only for subscription accounts).
        derived_mode = derive_auth_mode(managed_env)
        if derived_mode is AuthMode.NONE and status.logged_in and status.auth_method == "claude.ai":
            derived_mode = AuthMode.SUBSCRIPTION if status.subscription_type else AuthMode.CONSOLE
        return status.model_copy_update(
            to_update(status.field_ref().auth_mode, derived_mode),
            to_update(status.field_ref().masked_key_suffix, masked_credential_suffix(managed_env)),
            to_update(status.field_ref().workspace_id, read_workspace_id()),
        )























