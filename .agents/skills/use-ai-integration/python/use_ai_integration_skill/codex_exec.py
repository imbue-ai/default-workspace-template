import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, ValidationError

from use_ai_integration_skill.claude_p import Usage
from use_ai_integration_skill.data_types import CodexExecOutcome, CodexSandboxMode
from use_ai_integration_skill.errors import ProviderCallError, ProviderTimeoutError

# codex's own sandbox cannot run inside the workspace container, which is the isolation boundary
CODEX_TASK_SANDBOX: Final = CodexSandboxMode.DANGER_FULL_ACCESS

_STRIPPED_ENVIRONMENT_VARIABLES: Final = frozenset(
    {"OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "MNGR_AGENT_STATE_DIR"}
)
_CODEX_HOME_ENV_VAR: Final = "CODEX_HOME"
_STDERR_DETAIL_CHARS: Final = 500
_LINE_DETAIL_CHARS: Final = 200

_ERROR_EVENT: Final = "error"
_ITEM_COMPLETED_EVENT: Final = "item.completed"
_TURN_COMPLETED_EVENT: Final = "turn.completed"
_TURN_FAILED_EVENT: Final = "turn.failed"
_AGENT_MESSAGE_ITEM: Final = "agent_message"


class _CodexUsage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0


class _CodexItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str
    text: str | None = None


class _CodexErrorDetail(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message: str = ""


class _CodexEvent(BaseModel):
    """One line of ``codex exec --json`` output."""

    model_config = ConfigDict(extra="ignore")

    type: str
    item: _CodexItem | None = None
    usage: _CodexUsage | None = None
    error: _CodexErrorDetail | None = None
    message: str | None = None


def codex_exec_argv(*, model: str | None, sandbox_mode: CodexSandboxMode) -> list[str]:
    """The ``codex exec`` argv; the prompt goes on stdin (``-``), so a prompt starting with ``-`` is never a flag."""
    return [
        "codex",
        "exec",
        *(["--model", model] if model is not None else []),
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        sandbox_mode.value,
        "--color",
        "never",
        "--json",
        "-",
    ]


def codex_exec_environment(
    account_dir: Path, base_environment: Mapping[str, str]
) -> dict[str, str]:
    """``base_environment`` with codex bound to ``account_dir`` and no ambient credential to run on instead."""
    environment = {
        name: value
        for name, value in base_environment.items()
        if name not in _STRIPPED_ENVIRONMENT_VARIABLES
    }
    return {**environment, _CODEX_HOME_ENV_VAR: str(account_dir)}


def _decode_event(line: str) -> tuple[dict[str, object], _CodexEvent]:
    try:
        decoded = json.loads(line)
    except json.JSONDecodeError as e:
        raise ProviderCallError(
            f"codex exec printed a line that is not an event: {line[:_LINE_DETAIL_CHARS]!r}"
        ) from e
    try:
        event = _CodexEvent.model_validate(decoded)
    except ValidationError as e:
        raise ProviderCallError(
            f"codex exec printed a line that is not an event: {line[:_LINE_DETAIL_CHARS]!r}"
        ) from e
    return decoded, event


def parse_codex_exec_output(stdout: str) -> CodexExecOutcome:
    """The answer and usage of a ``codex exec --json`` run; an error a completed turn recovered from is not raised."""
    decoded_events = [
        _decode_event(line) for line in stdout.splitlines() if line.strip()
    ]
    events = [event for _, event in decoded_events]

    # A failed turn, or a turn that never completed, is the run's failure
    failed = next((event for event in events if event.type == _TURN_FAILED_EVENT), None)
    if failed is not None:
        detail = failed.error.message if failed.error is not None else ""
        raise ProviderCallError(f"codex exec turn failed: {detail}")
    completed = [event for event in events if event.type == _TURN_COMPLETED_EVENT]
    if not completed:
        error_messages = [
            event.message
            for event in events
            if event.type == _ERROR_EVENT and event.message
        ]
        suffix = f": {error_messages[-1]}" if error_messages else ""
        raise ProviderCallError(f"codex exec ended without completing its turn{suffix}")

    # The answer is the last agent message, and the usage the last completed turn's
    answers = [
        event.item.text
        for event in events
        if event.type == _ITEM_COMPLETED_EVENT
        and event.item is not None
        and event.item.type == _AGENT_MESSAGE_ITEM
        and event.item.text is not None
    ]
    if not answers or not answers[-1].strip():
        raise ProviderCallError("codex exec gave no answer")
    codex_usage = completed[-1].usage or _CodexUsage()
    usage = Usage(
        input_tokens=max(
            codex_usage.input_tokens
            - codex_usage.cached_input_tokens
            - codex_usage.cache_write_input_tokens,
            0,
        ),
        output_tokens=codex_usage.output_tokens,
        cache_read_tokens=codex_usage.cached_input_tokens,
        cache_write_tokens=codex_usage.cache_write_input_tokens,
    )
    return CodexExecOutcome(
        text=answers[-1], usage=usage, events=[decoded for decoded, _ in decoded_events]
    )


def _failure_detail(stdout: str, stderr: str) -> str:
    """What a failed run said: its failure event's message, then the end of its stderr."""
    stderr_tail = stderr.strip()[-_STDERR_DETAIL_CHARS:]
    try:
        parse_codex_exec_output(stdout)
    except ProviderCallError as e:
        return f"{e}; stderr: {stderr_tail}" if stderr_tail else str(e)
    return stderr_tail or "no output"


def run_codex_exec(
    prompt_text: str,
    *,
    account_dir: Path,
    model: str | None,
    sandbox_mode: CodexSandboxMode,
    working_dir: Path,
    timeout_seconds: float,
) -> CodexExecOutcome:
    """Run ``codex exec --json`` on the account with the prompt on stdin, then closed, and parse its events."""
    try:
        finished = subprocess.run(
            codex_exec_argv(model=model, sandbox_mode=sandbox_mode),
            input=prompt_text,
            capture_output=True,
            text=True,
            env=codex_exec_environment(account_dir, os.environ),
            cwd=working_dir,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise ProviderTimeoutError(
            f"codex exec did not finish within {e.timeout:g}s"
        ) from e
    except FileNotFoundError as e:
        raise ProviderCallError(f"could not start codex: {e}") from e
    if finished.returncode != 0:
        raise ProviderCallError(
            f"codex exec exited {finished.returncode}: {_failure_detail(finished.stdout, finished.stderr)}"
        )
    return parse_codex_exec_output(finished.stdout)
