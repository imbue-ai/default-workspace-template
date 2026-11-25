"""Self-contained helper for calling headless ``claude -p`` from a service.

New code calls ``use_ai_integration_skill.ai`` instead, which runs this module's ``claude -p``
path on an Anthropic sign-in; the file stays self-contained, so copies that services already
hold keep working.

It is the **keyless** path for an AI-driven service: with an ``ANTHROPIC_API_KEY``,
``use_ai_integration_skill.ai.complete()`` calls litellm with it. With no key, ``claude -p``
runs on the Claude plan the account is signed in with, and draws on that plan's usage limits.

Workspace credentials live in the ``env`` block of a provider account's
``settings.json`` under ``~/.minds/accounts/<id>/`` (the account
``$CLAUDE_CONFIG_DIR`` names inside an agent, else the workspace's default
account; written by the provider chooser), NOT in the process environment --
long-lived services inherit a frozen env from supervisord, so an env-var check
would go stale the moment the user changes auth. Keyed (API key) integrations
additionally snapshot the key + base URL into ``data/.secrets/anthropic.env`` at
setup time (``write_anthropic_env_snapshot``): the workspace's sign-in can
change after a service is built, and a keyed service keeps billing against the
key it was set up with rather than silently switching. The user removes or
re-snapshots that file (via the agent) to change an integration's key.
``read_workspace_ai_credentials`` resolves the snapshot first, then the
settings file, then the process env (the last for non-workspace contexts);
every fresh ``claude -p`` subprocess runs on that same account and reads its
settings itself, so the keyless path always uses current auth with no service
restarts. The subscription ``CLAUDE_CODE_OAUTH_TOKEN`` is never snapshotted --
it cannot authenticate direct API (litellm) calls.

Two entry points cover the two non-agent scenarios; both share one core that
handles the things that are easy to get wrong by hand:

- ``claude_p_completion(prompt, *, system, model=...)`` -- a non-agentic
  completion (classify / summarize / extract / rewrite / answer-from-context).
  Disables all tools (``--tools ""``) **and** runs from an isolated temp directory
  so ``claude -p`` does not auto-discover the repo's ``CLAUDE.md`` / ``.claude``
  hooks (which otherwise bleed into -- and intermittently hijack -- the answer).
  ``system`` is required: it frames the task and is the neutralizing instruction.
  (``--bare`` would also strip that project context, but it cannot authenticate
  without an API key, so the isolated cwd is the keyless workaround.) Its session
  is not persisted (``--no-session-persistence``): claude files a persisted
  session under a ``projects/`` directory named for the cwd, so every call would
  leave one more directory behind.

- ``claude_p_task(prompt, *, append_system=None, system=None, model=...,
  permission_mode="bypassPermissions")`` -- a one-shot agentic task that needs
  tools / file access. Tools stay enabled and it runs in ``cwd``, by default the
  current working directory (the repo). ``bypassPermissions`` is load-bearing: a headless run has
  no human to approve tool use, so otherwise Read/Write/Bash are auto-denied.

Both unset ``MAIN_CLAUDE_SESSION_ID`` in the child environment (an inherited value
makes the child look like mngr's managed main session and trips its
stop/readiness hooks), point the child at the resolved account
(``CLAUDE_CONFIG_DIR``), request ``--output-format json``, run the blocking
subprocess synchronously with its stdin closed and in its own process group (a
timeout ends every process it started), and raise on a non-zero exit or a
``claude -p`` error result rather than silently returning empty text.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, ValidationError

_MAIN_CLAUDE_SESSION_ID = "MAIN_CLAUDE_SESSION_ID"
# mngr identity vars its own subagent proxy strips; dropping them is defense in
# depth (the session-hook fix only needs MAIN_CLAUDE_SESSION_ID unset).
_MNGR_AGENT_VARS = ("MNGR_AGENT_STATE_DIR", "MNGR_AGENT_NAME", "MNGR_HOST_DIR")
# Credentials claude prefers over an account folder's own sign-in.
_AMBIENT_CREDENTIAL_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_OAUTH_TOKEN",
)
_DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"
# How long a process group has to exit after SIGTERM before it is sent SIGKILL.
_PROCESS_GROUP_GRACE_SECONDS = 2.0

_DEFAULT_MODEL = "claude-haiku-4-5"


class ClaudeCLIError(RuntimeError):
    """A ``claude -p`` invocation failed or returned unparseable / error output."""


class ClaudeCLITimeoutError(ClaudeCLIError):
    """A ``claude -p`` invocation did not finish within its timeout."""


@dataclass(frozen=True)
class WorkspaceAICredentials:
    """The workspace's current Anthropic credentials, resolved at call time.

    ``api_key`` present means the keyed (litellm-direct) path applies;
    ``base_url`` accompanies it for proxy (Imbue/LiteLLM) setups. With no
    key, use the keyless ``claude -p`` path (a subscription ``oauth_token``
    may be present but is consumed by claude itself, not by litellm).
    """

    api_key: str | None
    base_url: str | None
    oauth_token: str | None


# Where a keyed integration's snapshot of the workspace API key lives, written
# at integration-setup time by ``write_anthropic_env_snapshot``. Absolute, so
# the file lands in the gitignored ``data/.secrets/`` whatever directory the
# writer or a reader runs from: a relative path resolved against a skill or
# app directory puts the key in a file git sees, and Claude Code records a
# diff of every git-visible file a Bash command changes -- the key's full value
# included -- in the agent's session transcript. Holds ONLY ANTHROPIC_API_KEY
# (+ ANTHROPIC_BASE_URL): the subscription oauth token cannot authenticate
# direct API calls, so it is never written here.
ANTHROPIC_ENV_SNAPSHOT_PATH = "/home/user/workspace/data/.secrets/anthropic.env"


def _default_account_dir() -> str:
    """The workspace's default claude account's folder, or "" when there is none.

    Among the anthropic-lane accounts: the pinned default, else the most recently used,
    else the oldest -- the chat app's rule for an agent created without an account.
    Read straight from the index rather than imported from the chat app's package: this
    script runs from whatever environment the caller has, which is frequently not that
    venv. The format is one JSON object with an `accounts` list and optional
    `default_account` and `mru` ids.
    """
    root = os.path.join(os.path.expanduser("~"), ".minds", "accounts")
    try:
        with open(os.path.join(root, "index.json"), encoding="utf-8") as f:
            index = json.load(f)
        rows = [a for a in index.get("accounts", []) if a.get("lane") == "anthropic"]
        if not rows:
            return ""
        by_id = {a.get("id"): a for a in rows}
        chosen = (
            by_id.get(index.get("default_account"))
            or by_id.get(index.get("mru"))
            or rows[0]
        )
        path = os.path.join(root, str(chosen["id"]))
        return path if os.path.isdir(path) else ""
    except (OSError, ValueError, KeyError, TypeError):
        return ""


def _account_config_dir() -> str:
    """The account folder claude runs on, or "" when there is none.

    ``$CLAUDE_CONFIG_DIR`` when set, else the workspace's default account. Inside
    an agent the var IS set -- mngr sources the agent's env file into every
    process in its tmux session, so a skill script, a worker, and the agent
    itself all see the account the chat is bound to. Outside one -- a
    supervisord service, a cron job -- nothing sets it and ~/.claude holds no
    credential, so fall back to the workspace's default account rather than to
    nothing.
    """
    return os.environ.get("CLAUDE_CONFIG_DIR", "") or _default_account_dir()


def _read_env_file(path: str) -> dict[str, str]:
    """Parse simple ``KEY=VALUE`` lines from an env file; {} if absent/unreadable."""
    values: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip() and value.strip():
                    values[key.strip()] = value.strip()
    except OSError:
        return {}
    return values


def read_workspace_ai_credentials(
    snapshot_path: str = ANTHROPIC_ENV_SNAPSHOT_PATH,
    *,
    # the account folder whose settings are read, with the process env not consulted;
    # None for $CLAUDE_CONFIG_DIR, else the default account
    account_dir: str | None = None,
) -> WorkspaceAICredentials:
    """Resolve current credentials: the snapshot file, then account settings, then env.

    ``data/.secrets/anthropic.env`` -- the key snapshot a keyed integration
    writes at setup (``write_anthropic_env_snapshot``) -- wins for the API key
    and base URL: a built service stays pinned to the key it was set up with
    even after the user switches the workspace's sign-in in the modal. The
    account's settings.json env block (written by the provider chooser) is
    next; the process env is only a fallback so this helper still works outside
    a workspace (e.g. local development with an exported key), and is never read
    for a named ``account_dir``. The oauth token never comes from the snapshot
    (it is never written there).
    """
    snapshot_env = _read_env_file(snapshot_path)
    settings_env: dict[str, object] = {}
    config_dir = account_dir or _account_config_dir() or os.path.expanduser("~/.claude")
    settings_path = os.path.join(config_dir, "settings.json")
    try:
        with open(settings_path, encoding="utf-8") as f:
            settings = json.load(f)
        if isinstance(settings, dict) and isinstance(settings.get("env"), dict):
            settings_env = settings["env"]
    except (OSError, ValueError):
        settings_env = {}

    def resolve(key: str, use_snapshot: bool = True) -> str | None:
        if use_snapshot:
            snapshot_value = snapshot_env.get(key, "").strip()
            if snapshot_value:
                return snapshot_value
        settings_value = settings_env.get(key)
        if isinstance(settings_value, str) and settings_value.strip():
            return settings_value.strip()
        if account_dir is not None:
            return None
        env_value = os.environ.get(key, "").strip()
        return env_value or None

    # A snapshotted key is pinned to the snapshot's base URL, and to the direct API without one.
    is_key_snapshotted = bool(snapshot_env.get("ANTHROPIC_API_KEY", "").strip())
    return WorkspaceAICredentials(
        api_key=resolve("ANTHROPIC_API_KEY"),
        base_url=(
            snapshot_env.get("ANTHROPIC_BASE_URL", "").strip() or None
            if is_key_snapshotted
            else resolve("ANTHROPIC_BASE_URL")
        ),
        # The snapshot never legitimately holds a token (see the writer), so a
        # hand-edited one is ignored rather than trusted.
        oauth_token=resolve("CLAUDE_CODE_OAUTH_TOKEN", use_snapshot=False),
    )


def write_anthropic_env_snapshot(
    snapshot_path: str = ANTHROPIC_ENV_SNAPSHOT_PATH,
    *,
    # the account folder whose key is snapshotted; None for $CLAUDE_CONFIG_DIR, else the default account
    account_dir: str | None = None,
) -> str:
    """Snapshot the workspace's current API key (+ base URL) for a keyed integration.

    Run once at integration-setup time (and again only to deliberately
    re-key). Writes ``ANTHROPIC_API_KEY`` and, when present,
    ``ANTHROPIC_BASE_URL`` to ``data/.secrets/anthropic.env`` with owner-only
    permissions, creating ``data/.secrets/`` if needed. Deliberately never
    writes ``CLAUDE_CODE_OAUTH_TOKEN`` -- a subscription token cannot
    authenticate direct API calls. Raises when the workspace has no API key
    configured (the integration should use the keyless ``claude -p`` path
    instead). Returns the path written.
    """
    creds = read_workspace_ai_credentials(snapshot_path, account_dir=account_dir)
    if not creds.api_key:
        raise ClaudeCLIError(
            "No ANTHROPIC_API_KEY is configured for this workspace; there is nothing to snapshot. "
            "Use the keyless claude -p path instead."
        )
    lines = [f"ANTHROPIC_API_KEY={creds.api_key}"]
    if creds.base_url:
        lines.append(f"ANTHROPIC_BASE_URL={creds.base_url}")
    os.makedirs(os.path.dirname(snapshot_path), exist_ok=True)
    fd = os.open(snapshot_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return snapshot_path


@dataclass(frozen=True)
class Usage:
    """Token counts from a ``claude -p`` run, for cost / savings estimation."""

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class ClaudeResult:
    """The parsed result of a ``claude -p --output-format json`` run."""

    text: str
    cost_usd: float
    usage: Usage
    raw: dict[str, object]  # the verbatim JSON object claude -p emitted


def _child_env(
    strip_mngr_agent_vars: bool = False,
    *,
    # the account folder claude runs on, without the process env's Anthropic credentials;
    # None for $CLAUDE_CONFIG_DIR, else the default account
    account_dir: str | None = None,
) -> dict[str, str]:
    """Build the child environment: a copy of os.environ minus the session var.

    ``CLAUDE_CONFIG_DIR`` is set to ``account_dir``, else to the account the resolver reads.
    """
    env = dict(os.environ)
    env.pop(_MAIN_CLAUDE_SESSION_ID, None)
    if account_dir is not None:
        for var in _AMBIENT_CREDENTIAL_VARS:
            env.pop(var, None)
    config_dir = account_dir or _account_config_dir()
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    if strip_mngr_agent_vars:
        for var in _MNGR_AGENT_VARS:
            env.pop(var, None)
    return env


def _build_argv(
    # None when the prompt goes on stdin
    prompt: str | None,
    *,
    model: str,
    system: str | None,
    append_system: str | None,
    tools: str | None,
    permission_mode: str | None,
    is_session_persisted: bool,
    # flags placed before the prompt, e.g. ["--system-prompt-file", <path>]
    extra_flags: Sequence[str] = (),
) -> list[str]:
    """Assemble the ``claude -p`` argv. Pure, so flag emission is unit-testable.

    ``tools`` is checked against ``None`` (not falsiness): the empty string is the
    meaningful "disable every tool" value, distinct from "leave the flag off and
    inherit the default tool set".

    The prompt goes last, after ``--``: ``-p`` is a boolean flag, so the prompt is a
    positional argument, and without the terminator a prompt starting with ``-``
    (markdown frontmatter, a leading rule) is rejected as an unknown option.
    """
    argv = ["claude", "-p", "--output-format", "json", *extra_flags]
    if model:
        argv += ["--model", model]
    if system is not None:
        argv += ["--system-prompt", system]
    if append_system is not None:
        argv += ["--append-system-prompt", append_system]
    if tools is not None:
        argv += ["--tools", tools]
    if permission_mode is not None:
        argv += ["--permission-mode", permission_mode]
    if not is_session_persisted:
        argv.append("--no-session-persistence")
    return argv if prompt is None else argv + ["--", prompt]


def _completion_argv(
    prompt: str | None,
    *,
    system: str | None,
    model: str,
    extra_flags: Sequence[str] = (),
) -> list[str]:
    """The argv of one non-agentic completion.

    Not persisted: every completion runs from its own throwaway cwd, and claude files a
    persisted session under a ``projects/`` directory named for that cwd, so each call
    would leave a directory behind in the account's projects tree for good.
    """
    return _build_argv(
        prompt,
        model=model,
        system=system,
        append_system=None,
        tools="",
        permission_mode=None,
        is_session_persisted=False,
        extra_flags=extra_flags,
    )


def _task_argv(
    prompt: str | None,
    *,
    system: str | None,
    append_system: str | None,
    model: str,
    permission_mode: str | None,
    extra_flags: Sequence[str] = (),
) -> list[str]:
    """The argv of one agentic task, persisted like any session run from the repo."""
    return _build_argv(
        prompt,
        model=model,
        system=system,
        append_system=append_system,
        tools=None,
        permission_mode=permission_mode,
        is_session_persisted=True,
        extra_flags=extra_flags,
    )


class _UsageModel(BaseModel):
    """The ``usage`` block of a ``claude -p`` result, with token counts validated.

    Extra keys are ignored (the block carries fields we do not surface), and each
    count defaults to 0 so an absent field is fine; a present value that cannot be
    read as an integer fails validation rather than silently reading as 0.
    """

    model_config = ConfigDict(extra="ignore")

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


class _ResultModel(BaseModel):
    """A ``claude -p --output-format json`` result message, typed and validated.

    The fields are optional with defaults because the payload shape differs by arm:
    the **success arm** (``subtype == "success"``) carries ``result`` and
    ``total_cost_usd``, while the **error arm** (``is_error`` true, e.g.
    ``error_max_turns`` / ``error_during_execution``) carries ``errors`` and no
    ``result``. ``_parse_result`` decides the arm and rejects the error arm or a
    success arm missing its text. pydantic enforces each field's *type*, so a
    wrong-typed ``result`` or ``total_cost_usd`` (or a non-object payload) raises
    instead of slipping through.
    """

    model_config = ConfigDict(extra="ignore")

    subtype: str | None = None
    is_error: bool = False
    result: str | None = None
    total_cost_usd: float | None = None
    usage: _UsageModel = Field(default_factory=_UsageModel)
    errors: list[object] = Field(default_factory=list)


def _parse_result(data: object) -> ClaudeResult:
    """Validate raw ``claude -p`` JSON into a ``ClaudeResult``, or raise.

    ``data`` is the verbatim decoded JSON (any shape). It is validated into the
    typed ``_ResultModel`` first -- a non-object payload or a wrong-typed field
    raises ``ClaudeCLIError`` -- so the rest of this function reads typed
    attributes rather than poking at an untyped object. The error arm and a
    success arm with no ``result`` text both raise, so a maxed-out or failed run
    surfaces instead of looking like an empty-text success.
    """
    try:
        payload = _ResultModel.model_validate(data)
    except ValidationError as exc:
        raise ClaudeCLIError(
            f"claude -p JSON did not match the expected result shape: {exc}"
        ) from exc
    if payload.is_error or payload.subtype != "success":
        detail = "; ".join(str(error) for error in payload.errors)
        raise ClaudeCLIError(
            f"claude -p returned an error result (subtype={payload.subtype!r}): "
            f"{detail or 'no error detail reported'}"
        )
    if payload.result is None:
        raise ClaudeCLIError("claude -p success result was missing the 'result' text")
    if payload.total_cost_usd is None:
        raise ClaudeCLIError("claude -p result was missing a numeric 'total_cost_usd'")
    usage = Usage(
        input_tokens=payload.usage.input_tokens,
        output_tokens=payload.usage.output_tokens,
        cache_read_tokens=payload.usage.cache_read_input_tokens,
        cache_write_tokens=payload.usage.cache_creation_input_tokens,
    )
    raw = dict(data) if isinstance(data, dict) else {}
    return ClaudeResult(
        text=payload.result, cost_usd=payload.total_cost_usd, usage=usage, raw=raw
    )


def _write_private_file(directory: str, name: str, text: str) -> str:
    """Write ``text`` to an owner-only file ``name`` in ``directory``; return its path."""
    path = os.path.join(directory, name)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return path


@dataclass(frozen=True)
class _PromptPlacement:
    """Where a run's prompt texts go: inline in argv, or on stdin and in private files."""

    prompt: str | None
    system: str | None
    append_system: str | None
    flags: tuple[str, ...]
    stdin_text: str | None


def _place_prompt_text(
    prompt: str,
    *,
    system: str | None,
    append_system: str | None,
    # the directory the system texts are written to when they are kept out of argv
    files_dir: str,
    is_prompt_text_outside_argv: bool,
) -> _PromptPlacement:
    if not is_prompt_text_outside_argv:
        return _PromptPlacement(
            prompt=prompt,
            system=system,
            append_system=append_system,
            flags=(),
            stdin_text=None,
        )
    flags: list[str] = []
    if system is not None:
        flags += [
            "--system-prompt-file",
            _write_private_file(files_dir, "system-prompt.txt", system),
        ]
    if append_system is not None:
        flags += [
            "--append-system-prompt-file",
            _write_private_file(files_dir, "append-system-prompt.txt", append_system),
        ]
    return _PromptPlacement(
        prompt=None,
        system=None,
        append_system=None,
        flags=tuple(flags),
        stdin_text=prompt,
    )


def _api_key_settings(api_credentials: WorkspaceAICredentials) -> str:
    """A settings file's JSON that authenticates claude with the key and its base URL."""
    return json.dumps(
        {
            "env": {
                "ANTHROPIC_API_KEY": api_credentials.api_key,
                "ANTHROPIC_BASE_URL": api_credentials.base_url
                or _DEFAULT_ANTHROPIC_BASE_URL,
            }
        }
    )


def _end_process_group(process: subprocess.Popen[str]) -> None:
    """Signal ``process``'s whole group with SIGTERM, then SIGKILL, collecting what it printed."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        # macOS answers EPERM for a group whose members have all exited.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, sig)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.communicate(timeout=_PROCESS_GROUP_GRACE_SECONDS)


def run_in_own_process_group(
    argv: Sequence[str],
    *,
    # written to stdin, which is then closed; None for a closed stdin from the start
    input_text: str | None,
    env: Mapping[str, str],
    cwd: str | None,
    timeout_seconds: float | None,
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` to completion in its own process group, decoding its output as UTF-8.

    On a timeout, or any other exception while it runs, the whole group is ended, so no
    process it started outlives the call; the timeout raises ``subprocess.TimeoutExpired``.
    """
    with subprocess.Popen(
        list(argv),
        # A service's inherited stdin never closes, and claude -p waits 3s for it.
        stdin=subprocess.DEVNULL if input_text is None else subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        env=dict(env),
        cwd=cwd,
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(input_text, timeout=timeout_seconds)
        except BaseException:
            _end_process_group(process)
            raise
    return subprocess.CompletedProcess(list(argv), process.returncode, stdout, stderr)


def _run_blocking(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: str | None,
    timeout_seconds: float | None = None,
    # written to claude's stdin; None for a closed stdin
    input_text: str | None = None,
) -> ClaudeResult:
    """Run ``claude -p`` synchronously and parse its JSON. Raises on failure."""
    try:
        proc = run_in_own_process_group(
            argv,
            input_text=input_text,
            env=env,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise ClaudeCLITimeoutError(
            f"claude -p did not finish within {exc.timeout:g}s"
        ) from exc
    if proc.returncode != 0:
        raise ClaudeCLIError(
            f"claude -p exited {proc.returncode}: {proc.stderr.strip()[:500]}"
        )
    try:
        decoded = json.loads(proc.stdout)
    except ValueError as exc:
        raise ClaudeCLIError(f"claude -p output was not valid JSON: {exc}") from exc
    return _parse_result(decoded)


def claude_p_completion(
    prompt: str,
    *,
    system: str,
    model: str = _DEFAULT_MODEL,
    strip_mngr_agent_vars: bool = False,
    # the account folder claude runs on; None for $CLAUDE_CONFIG_DIR, else the default account
    account_dir: str | None = None,
    timeout_seconds: float | None = None,
    # send the prompt on stdin and the system prompt in a file, so neither is bounded by
    # the OS's limit on one argument's length
    is_prompt_text_outside_argv: bool = False,
) -> ClaudeResult:
    """One non-agentic completion. ``system`` is required (see module docstring)."""
    env = _child_env(strip_mngr_agent_vars, account_dir=account_dir)
    # Isolated cwd: claude -p auto-discovers CLAUDE.md / .claude hooks from the
    # working directory, so a throwaway dir keeps that project context out of the
    # answer. Credentials come from the env, not the cwd, so auth is unaffected.
    with (
        tempfile.TemporaryDirectory(prefix="claude_p_completion_") as cwd,
        tempfile.TemporaryDirectory(prefix="claude_p_files_") as files_dir,
    ):
        placed = _place_prompt_text(
            prompt,
            system=system,
            append_system=None,
            files_dir=files_dir,
            is_prompt_text_outside_argv=is_prompt_text_outside_argv,
        )
        argv = _completion_argv(
            placed.prompt, system=placed.system, model=model, extra_flags=placed.flags
        )
        return _run_blocking(
            argv,
            env=env,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            input_text=placed.stdin_text,
        )


def claude_p_task(
    prompt: str,
    *,
    system: str | None = None,
    append_system: str | None = None,
    model: str = _DEFAULT_MODEL,
    permission_mode: str | None = "bypassPermissions",
    strip_mngr_agent_vars: bool = False,
    # the account folder claude runs on; None for $CLAUDE_CONFIG_DIR, else the default account
    account_dir: str | None = None,
    # an API key (and its base URL) claude authenticates with, over the account's own settings
    api_credentials: WorkspaceAICredentials | None = None,
    # the directory the task runs in; None for the current working directory
    cwd: str | None = None,
    timeout_seconds: float | None = None,
    # send the prompt on stdin and the system texts in files, so none is bounded by the
    # OS's limit on one argument's length
    is_prompt_text_outside_argv: bool = False,
) -> ClaudeResult:
    """One agentic task: tools enabled, run in ``cwd`` (by default the current, repo, working dir).

    ``append_system`` layers task instructions on Claude Code's default agent
    prompt; pass ``system`` to replace it outright (rare -- you usually want the
    default agent here).
    """
    env = _child_env(strip_mngr_agent_vars, account_dir=account_dir)
    with tempfile.TemporaryDirectory(prefix="claude_p_files_") as files_dir:
        placed = _place_prompt_text(
            prompt,
            system=system,
            append_system=append_system,
            files_dir=files_dir,
            is_prompt_text_outside_argv=is_prompt_text_outside_argv,
        )
        # --settings outranks the account's own settings.json env, which outranks the process env.
        settings_flags = (
            (
                "--settings",
                _write_private_file(
                    files_dir, "settings.json", _api_key_settings(api_credentials)
                ),
            )
            if api_credentials is not None and api_credentials.api_key
            else ()
        )
        argv = _task_argv(
            placed.prompt,
            system=placed.system,
            append_system=placed.append_system,
            model=model,
            permission_mode=permission_mode,
            extra_flags=(*settings_flags, *placed.flags),
        )
        return _run_blocking(
            argv,
            env=env,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            input_text=placed.stdin_text,
        )
