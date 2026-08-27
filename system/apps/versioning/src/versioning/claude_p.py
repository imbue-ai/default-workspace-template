"""Helpers for calling headless ``claude -p`` (the keyless AI path).

Adapted from the use-ai-integration skill's reference snippet
(``.agents/skills/use-ai-integration/scripts/claude_p.py``), trimmed to the
two entry points this service uses: ``claude_p_completion`` and ``claude_p_task``.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Mapping
from collections.abc import Sequence

from imbue.imbue_common.frozen_model import FrozenModel
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import ValidationError

_MAIN_CLAUDE_SESSION_ID = "MAIN_CLAUDE_SESSION_ID"

_DEFAULT_MODEL = "claude-haiku-4-5"


class ClaudeCLIError(RuntimeError):
    """A ``claude -p`` invocation failed or returned unparseable / error output."""


class Usage(FrozenModel):
    """Token counts from a ``claude -p`` run, for cost / savings estimation."""

    input_tokens: int = Field(description="Tokens sent in the prompt")
    output_tokens: int = Field(description="Tokens produced in the response")
    cache_read_tokens: int = Field(default=0, description="Tokens served from cache")
    cache_write_tokens: int = Field(default=0, description="Tokens written to cache")


class ClaudeResult(FrozenModel):
    """The parsed result of a ``claude -p --output-format json`` run."""

    text: str = Field(description="The response text")
    cost_usd: float = Field(description="What this call cost, as claude -p reported it")
    usage: Usage = Field(description="Token counts for the call")
    raw: dict[str, object] = Field(
        description="The verbatim JSON object claude -p emitted"
    )


def _child_env() -> dict[str, str]:
    """Build the child environment: a copy of os.environ minus the session var."""
    # An inherited MAIN_CLAUDE_SESSION_ID makes the child look like mngr's managed
    # main session and trips its stop/readiness hooks, so it must be unset.
    env = dict(os.environ)
    env.pop(_MAIN_CLAUDE_SESSION_ID, None)
    return env


def _build_argv(
    prompt: str,
    *,
    model: str,
    system: str | None,
    append_system: str | None,
    tools: str | None,
    permission_mode: str | None,
) -> list[str]:
    """Assemble the ``claude -p`` argv. Pure, so flag emission is unit-testable.

    ``tools`` is checked against ``None`` (not falsiness): the empty string is the
    meaningful "disable every tool" value, distinct from "leave the flag off and
    inherit the default tool set".
    """
    argv = ["claude", "-p", prompt, "--output-format", "json"]
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
    return argv


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


def _run_blocking(
    argv: Sequence[str], *, env: Mapping[str, str], cwd: str | None
) -> ClaudeResult:
    """Run ``claude -p`` synchronously and parse its JSON. Raises on failure."""
    proc = subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        env=dict(env),
        check=False,
        cwd=cwd,
    )
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
) -> ClaudeResult:
    """One non-agentic completion. ``system`` is required: it frames the task."""
    env = _child_env()
    argv = _build_argv(
        prompt,
        model=model,
        system=system,
        append_system=None,
        tools="",
        permission_mode=None,
    )
    # Isolated cwd: claude -p auto-discovers CLAUDE.md / .claude hooks from the
    # working directory, so a throwaway dir keeps that project context out of the
    # answer. Credentials come from the env, not the cwd, so auth is unaffected.
    with tempfile.TemporaryDirectory(prefix="claude_p_completion_") as cwd:
        return _run_blocking(argv, env=env, cwd=cwd)


def claude_p_task(
    prompt: str,
    *,
    system: str | None = None,
    append_system: str | None = None,
    model: str = _DEFAULT_MODEL,
    # bypassPermissions is load-bearing: a headless run has no human to approve
    # tool use, so otherwise Read/Write/Bash are auto-denied.
    permission_mode: str | None = "bypassPermissions",
    # None inherits the full default tool set; a comma-separated list restricts it.
    tools: str | None = None,
) -> ClaudeResult:
    """One agentic task: tools enabled, run in the current (repo) working dir.

    ``append_system`` layers task instructions on Claude Code's default agent
    prompt; pass ``system`` to replace it outright (rare -- you usually want the
    default agent here).
    """
    env = _child_env()
    argv = _build_argv(
        prompt,
        model=model,
        system=system,
        append_system=append_system,
        tools=tools,
        permission_mode=permission_mode,
    )
    return _run_blocking(argv, env=env, cwd=None)
