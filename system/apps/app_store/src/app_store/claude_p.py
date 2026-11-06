"""Calling headless ``claude -p`` from the store: one non-agentic completion, with a time limit.

The store's copy of the workspace's shared helper (``.agents/skills/use-ai-integration/scripts/claude_p.py``),
trimmed to the one scenario the store uses -- ``claude_p_completion``, which the command finder asks for each start
command -- and given the time limit a service needs so a stuck call never leaves a dialog waiting.

``claude_p_completion(prompt, *, system, model=..., timeout_seconds=None)`` disables every tool (``--tools ""``)
**and** runs from an isolated temp directory, so ``claude -p`` does not auto-discover the repo's ``CLAUDE.md`` /
``.claude`` hooks (which otherwise bleed into -- and intermittently hijack -- the answer). ``system`` is required: it
frames the task and is the neutralizing instruction. (``--bare`` would also strip that project context, but it cannot
authenticate without an API key, so the isolated cwd is the keyless workaround.) Its session is not persisted
(``--no-session-persistence``): claude files a persisted session under a ``projects/`` directory named for the cwd,
so every call would leave one more directory behind.

The store reads no credential itself. The child runs on the workspace's Claude account (``CLAUDE_CONFIG_DIR``: the
caller's own when set, else the workspace's default account under ``~/.minds/accounts/``), and claude reads that
account's settings itself on every call, so a change of sign-in applies with no restart. The child's environment
drops ``MAIN_CLAUDE_SESSION_ID`` (an inherited value makes the child look like mngr's managed main session and trips
its stop/readiness hooks); the call requests ``--output-format json``, runs synchronously with its stdin closed, and
raises ``ClaudeCLIError`` on a non-zero exit, a timeout, a missing ``claude``, or an error result rather than
silently returning empty text.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Mapping
from collections.abc import Sequence

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel

_MAIN_CLAUDE_SESSION_ID = "MAIN_CLAUDE_SESSION_ID"

_DEFAULT_MODEL = "claude-haiku-4-5"


class ClaudeCLIError(RuntimeError):
    """A ``claude -p`` invocation failed or returned unparseable / error output."""


def _default_account_dir() -> str:
    """The workspace's default claude account's folder, or "" when there is none.

    Among the anthropic-lane accounts: the pinned default, else the most recently used,
    else the oldest -- the chat app's rule for an agent created without an account.
    Read straight from the index rather than imported from the chat app's package, which
    the store does not depend on. The format is one JSON object with an `accounts` list and optional
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
        chosen = by_id.get(index.get("default_account")) or by_id.get(index.get("mru")) or rows[0]
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


class Usage(FrozenModel):
    """Token counts from a ``claude -p`` run, for cost / savings estimation."""

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class ClaudeResult(FrozenModel):
    """The parsed result of a ``claude -p --output-format json`` run."""

    text: str
    cost_usd: float
    usage: Usage
    raw: dict[str, object]  # the verbatim JSON object claude -p emitted


def _child_env() -> dict[str, str]:
    """Build the child environment: a copy of os.environ minus the session var, with
    ``CLAUDE_CONFIG_DIR`` set to the account claude runs on (when there is one)."""
    env = dict(os.environ)
    env.pop(_MAIN_CLAUDE_SESSION_ID, None)
    config_dir = _account_config_dir()
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    return env


def _completion_argv(prompt: str, *, system: str, model: str) -> list[str]:
    """The argv of one non-agentic completion. Pure, so flag emission is unit-testable.

    ``--tools ""`` disables every tool. Not persisted: every completion runs from its own
    throwaway cwd, and claude files a persisted session under a ``projects/`` directory
    named for that cwd, so each call would leave a directory behind in the account's
    projects tree for good.

    The prompt goes last, after ``--``: ``-p`` is a boolean flag, so the prompt is a
    positional argument, and without the terminator a prompt starting with ``-``
    (markdown frontmatter, a leading rule) is rejected as an unknown option.
    """
    argv = ["claude", "-p", "--output-format", "json"]
    if model:
        argv += ["--model", model]
    argv += ["--system-prompt", system, "--tools", "", "--no-session-persistence"]
    return argv + ["--", prompt]


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
        raise ClaudeCLIError(f"claude -p JSON did not match the expected result shape: {exc}") from exc
    if payload.is_error or payload.subtype != "success":
        detail = "; ".join(str(error) for error in payload.errors)
        raise ClaudeCLIError(
            f"claude -p returned an error result (subtype={payload.subtype!r}): {detail or 'no error detail reported'}"
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
    return ClaudeResult(text=payload.result, cost_usd=payload.total_cost_usd, usage=usage, raw=raw)


def _run_blocking(
    argv: Sequence[str], *, env: Mapping[str, str], cwd: str, timeout_seconds: float | None
) -> ClaudeResult:
    """Run ``claude -p`` synchronously and parse its JSON. Raises on failure, on a missing ``claude``, and when it
    has not finished within ``timeout_seconds`` (None waits for as long as it takes)."""
    try:
        proc = subprocess.run(
            list(argv),
            # A service's inherited stdin never closes, and claude -p waits 3s for it.
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=dict(env),
            check=False,
            cwd=cwd,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise ClaudeCLIError(f"claude -p did not finish within {timeout_seconds:g}s") from exc
    except OSError as exc:
        raise ClaudeCLIError(f"claude -p could not be run: {exc}") from exc
    if proc.returncode != 0:
        raise ClaudeCLIError(f"claude -p exited {proc.returncode}: {proc.stderr.strip()[:500]}")
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
    timeout_seconds: float | None = None,
) -> ClaudeResult:
    """One non-agentic completion. ``system`` is required (see module docstring). A service that must not hang on
    a stuck call passes ``timeout_seconds``."""
    env = _child_env()
    argv = _completion_argv(prompt, system=system, model=model)
    # Isolated cwd: claude -p auto-discovers CLAUDE.md / .claude hooks from the
    # working directory, so a throwaway dir keeps that project context out of the
    # answer. Credentials come from the env, not the cwd, so auth is unaffected.
    with tempfile.TemporaryDirectory(prefix="claude_p_completion_") as cwd:
        return _run_blocking(argv, env=env, cwd=cwd, timeout_seconds=timeout_seconds)
