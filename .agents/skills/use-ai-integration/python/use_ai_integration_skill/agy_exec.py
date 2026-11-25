import json
import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from use_ai_integration_skill.claude_p import Usage, run_in_own_process_group
from use_ai_integration_skill.data_types import AgyRunOutcome
from use_ai_integration_skill.errors import ProviderCallError, ProviderTimeoutError

_HOME_ENV_VAR: Final = "HOME"
_PWD_ENV_VAR: Final = "PWD"
_AGY_DISABLE_AUTO_UPDATE_ENV_VAR: Final = "AGY_CLI_DISABLE_AUTO_UPDATE"
# Every other way agy 1.5.8 can sign in, so the account folder's sign-in is the only one it can use
_CREDENTIAL_ENVIRONMENT_VARIABLES: Final = frozenset(
    {
        "AGY_ADC_AUTH",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_GEMINI_BASE_URL",
        "GOOGLE_GENAI_USE_ENTERPRISE",
    }
)
_STRIPPED_ENVIRONMENT_VARIABLES: Final = _CREDENTIAL_ENVIRONMENT_VARIABLES | {
    "MNGR_AGENT_STATE_DIR"
}
# agy's Default mode, with every command and file edit asking for an approval no one can give
_COMPLETION_SETTINGS: Final = '{"agentMode":"","toolPermission":"strict"}'
_PRINT_TIMEOUT_EXIT_CODE: Final = 3
_PRINT_TIMEOUT_FINISHED_REASON: Final = "print_timeout"
_SUCCESS_STATUS: Final = "SUCCESS"
_SHUTDOWN_MARGIN_SECONDS: Final = 10.0
# A parent with no dot-prefixed segment, whatever TMPDIR says
_SCRATCH_PARENT: Final = Path("/tmp")
_PROJECT_LINK_NAME: Final = "workspace"
_STDERR_DETAIL_CHARS: Final = 500
_LINE_DETAIL_CHARS: Final = 200


class _AgyUsage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cache_read_tokens: int = 0


class _AgyEnvelope(BaseModel):
    """The result ``agy --output-format json`` prints."""

    model_config = ConfigDict(extra="ignore")

    status: str
    response: str = ""
    error: str = ""
    finished_reason: str | None = None
    usage: _AgyUsage = Field(default_factory=_AgyUsage)


def agy_print_timeout_seconds(timeout_seconds: float) -> float:
    """agy's own turn limit: the call's timeout less a shutdown margin, and never under half of it."""
    return max(timeout_seconds - _SHUTDOWN_MARGIN_SECONDS, timeout_seconds / 2)


def agy_completion_argv(
    *, model: str | None, print_timeout_seconds: float
) -> list[str]:
    """A print-mode run in agy's Default mode with strict tool permissions; the prompt goes on stdin."""
    return [
        "agy",
        "--output-format=json",
        "--disable-slash-commands",
        f"--print-timeout={print_timeout_seconds:.3f}s",
        f"--settings={_COMPLETION_SETTINGS}",
        *([f"--model={model}"] if model is not None else []),
    ]


def agy_task_argv(*, model: str | None, print_timeout_seconds: float) -> list[str]:
    """A print-mode run with every tool approved; the prompt goes on stdin."""
    return [
        "agy",
        "--output-format=json",
        "--disable-slash-commands",
        f"--print-timeout={print_timeout_seconds:.3f}s",
        "--dangerously-skip-permissions",
        *([f"--model={model}"] if model is not None else []),
    ]


def agy_environment(
    account_dir: Path, run_dir: Path, base_environment: Mapping[str, str]
) -> dict[str, str]:
    """``base_environment`` with agy's home set to ``account_dir``, its self-updater off, and no other Google credential."""
    environment = {
        name: value
        for name, value in base_environment.items()
        if name not in _STRIPPED_ENVIRONMENT_VARIABLES
    }
    return {
        **environment,
        _HOME_ENV_VAR: str(account_dir),
        _AGY_DISABLE_AUTO_UPDATE_ENV_VAR: "true",
        _PWD_ENV_VAR: str(run_dir),
    }


def agy_run_dir(working_dir: Path, scratch_dir: Path) -> Path:
    """``working_dir``, or a link to it inside ``scratch_dir`` when a segment of its path starts with a dot."""
    project_dir = Path(os.path.abspath(working_dir))
    if not any(part.startswith(".") for part in project_dir.parts):
        return project_dir
    # agy takes no folder with a dot-prefixed segment as its project, and falls back to its home
    link = scratch_dir / _PROJECT_LINK_NAME
    link.symlink_to(project_dir)
    return link


def _find_envelope(stdout: str) -> tuple[dict[str, object], _AgyEnvelope] | None:
    """The last stdout line that is agy's JSON result, decoded and validated."""
    for line in reversed(stdout.splitlines()):
        try:
            decoded = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            envelope = _AgyEnvelope.model_validate(decoded)
        except ValidationError:
            continue
        if isinstance(decoded, dict):
            return decoded, envelope
    return None


def parse_agy_output(stdout: str) -> AgyRunOutcome:
    """The response and usage of an ``agy --output-format json`` run that exited 0."""
    found = _find_envelope(stdout)
    if found is None:
        raise ProviderCallError(
            f"agy printed no result: {stdout.strip()[-_LINE_DETAIL_CHARS:]!r}"
        )
    decoded, envelope = found
    if envelope.finished_reason == _PRINT_TIMEOUT_FINISHED_REASON:
        raise ProviderTimeoutError("agy hit its print timeout; its answer is partial")
    if envelope.status != _SUCCESS_STATUS:
        raise ProviderCallError(
            f"agy ended with status {envelope.status}: {envelope.error or 'no error reported'}"
        )
    if not envelope.response.strip():
        raise ProviderCallError("agy gave no answer")
    usage = Usage(
        input_tokens=max(
            envelope.usage.input_tokens - envelope.usage.cache_read_tokens, 0
        ),
        output_tokens=envelope.usage.output_tokens + envelope.usage.thinking_tokens,
        cache_read_tokens=envelope.usage.cache_read_tokens,
        cache_write_tokens=0,
    )
    return AgyRunOutcome(text=envelope.response, usage=usage, envelope=decoded)


def _failure_detail(stdout: str, stderr: str) -> str:
    """What a failed run said: its result's error, then the end of its stderr."""
    stderr_tail = stderr.strip()[-_STDERR_DETAIL_CHARS:]
    found = _find_envelope(stdout)
    error = found[1].error if found is not None else ""
    if error and stderr_tail:
        return f"{error}; stderr: {stderr_tail}"
    elif error:
        return error
    else:
        return stderr_tail or "no output"


def run_agy(
    argv: Sequence[str],
    *,
    prompt_text: str,
    account_dir: Path,
    run_dir: Path,
    timeout_seconds: float,
) -> AgyRunOutcome:
    """Run agy in print mode with the prompt on stdin, then closed, and parse its JSON result."""
    # agy reads a print-mode prompt only from a non-empty stdin; an empty one starts its terminal UI
    if not prompt_text.strip():
        raise ProviderCallError("agy cannot be sent an empty prompt")
    try:
        finished = run_in_own_process_group(
            argv,
            input_text=prompt_text,
            env=agy_environment(account_dir, run_dir, os.environ),
            cwd=str(run_dir),
            timeout_seconds=timeout_seconds,
        )
    except subprocess.TimeoutExpired as e:
        raise ProviderTimeoutError(f"agy did not finish within {e.timeout:g}s") from e
    except OSError as e:
        raise ProviderCallError(f"could not start agy: {e}") from e
    if finished.returncode == _PRINT_TIMEOUT_EXIT_CODE:
        raise ProviderTimeoutError(
            f"agy hit its print timeout: {_failure_detail(finished.stdout, finished.stderr)}"
        )
    if finished.returncode != 0:
        raise ProviderCallError(
            f"agy exited {finished.returncode}: {_failure_detail(finished.stdout, finished.stderr)}"
        )
    return parse_agy_output(finished.stdout)


def agy_complete(
    prompt: str,
    *,
    system: str,
    model: str | None,
    account_dir: Path,
    timeout_seconds: float,
) -> AgyRunOutcome:
    """One agy turn from an empty directory outside the repo, with ``system`` leading the prompt."""
    with tempfile.TemporaryDirectory(prefix="agy_", dir=_SCRATCH_PARENT) as scratch:
        run_dir = Path(scratch, "cwd")
        run_dir.mkdir()
        return run_agy(
            agy_completion_argv(
                model=model,
                print_timeout_seconds=agy_print_timeout_seconds(timeout_seconds),
            ),
            prompt_text=f"{system}\n\n{prompt}",
            account_dir=account_dir,
            run_dir=run_dir,
            timeout_seconds=timeout_seconds,
        )


def agy_run_task(
    prompt: str,
    *,
    instructions: str | None,
    model: str | None,
    account_dir: Path,
    working_dir: Path,
    timeout_seconds: float,
) -> AgyRunOutcome:
    """One agy task with every tool approved in ``working_dir``, ``instructions`` leading the prompt."""
    with tempfile.TemporaryDirectory(prefix="agy_", dir=_SCRATCH_PARENT) as scratch:
        return run_agy(
            agy_task_argv(
                model=model,
                print_timeout_seconds=agy_print_timeout_seconds(timeout_seconds),
            ),
            prompt_text=f"{instructions}\n\n{prompt}" if instructions else prompt,
            account_dir=account_dir,
            run_dir=agy_run_dir(working_dir, Path(scratch)),
            timeout_seconds=timeout_seconds,
        )
