import json
import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from use_ai_integration_skill.claude_p import Usage, run_in_own_process_group
from use_ai_integration_skill.data_types import PiRunOutcome
from use_ai_integration_skill.errors import ProviderCallError, ProviderTimeoutError

PI_AGENT_DIR_ENV_VAR: Final = "PI_CODING_AGENT_DIR"

_STDERR_DETAIL_CHARS: Final = 500
_LINE_DETAIL_CHARS: Final = 200
_SYSTEM_PROMPT_FILENAME: Final = "system-prompt.txt"
_INSTRUCTIONS_FILENAME: Final = "instructions.txt"
_MESSAGE_END_EVENT: Final = "message_end"
_ASSISTANT_ROLE: Final = "assistant"
_TEXT_BLOCK: Final = "text"
_FAILED_STOP_REASONS: Final = frozenset({"error", "aborted"})

# Every credential variable pi 0.87.1 reads, so the account folder's key is the only one it can use
_CREDENTIAL_ENVIRONMENT_PREFIXES: Final = ("AWS_",)
_CREDENTIAL_ENVIRONMENT_VARIABLES: Final = frozenset(
    {
        "AI_GATEWAY_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_OAUTH_TOKEN",
        "ANT_LING_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "BASETEN_API_KEY",
        "CEREBRAS_API_KEY",
        "CLOUDFLARE_API_KEY",
        "COPILOT_GITHUB_TOKEN",
        "DEEPSEEK_API_KEY",
        "FIREWORKS_API_KEY",
        "GCLOUD_PROJECT",
        "GEMINI_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_API_KEY",
        "GOOGLE_CLOUD_LOCATION",
        "GOOGLE_CLOUD_PROJECT",
        "GROQ_API_KEY",
        "HF_TOKEN",
        "KIMI_API_KEY",
        "META_API_KEY",
        "MINIMAX_API_KEY",
        "MINIMAX_CN_API_KEY",
        "MISTRAL_API_KEY",
        "MOONSHOT_API_KEY",
        "NVIDIA_API_KEY",
        "OPENAI_API_KEY",
        "OPENCODE_API_KEY",
        "OPENROUTER_API_KEY",
        "QWEN_TOKEN_PLAN_API_KEY",
        "QWEN_TOKEN_PLAN_CN_API_KEY",
        "RADIUS_API_KEY",
        "TOGETHER_API_KEY",
        "XAI_API_KEY",
        "XIAOMI_API_KEY",
        "XIAOMI_TOKEN_PLAN_AMS_API_KEY",
        "XIAOMI_TOKEN_PLAN_CN_API_KEY",
        "XIAOMI_TOKEN_PLAN_SGP_API_KEY",
        "ZAI_API_KEY",
        "ZAI_CODING_CN_API_KEY",
    }
)
_STRIPPED_ENVIRONMENT_VARIABLES: Final = _CREDENTIAL_ENVIRONMENT_VARIABLES | {
    "MNGR_AGENT_STATE_DIR"
}


class _PiCost(BaseModel):
    model_config = ConfigDict(extra="ignore")

    total: float = 0.0


class _PiUsage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    input: int = 0
    output: int = 0
    cache_read: int = Field(default=0, alias="cacheRead")
    cache_write: int = Field(default=0, alias="cacheWrite")
    cost: _PiCost = Field(default_factory=_PiCost)


class _PiContentBlock(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str
    text: str | None = None


class _PiMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: str
    content: str | list[_PiContentBlock] = ""
    usage: _PiUsage = Field(default_factory=_PiUsage)
    stop_reason: str | None = Field(default=None, alias="stopReason")
    error_message: str | None = Field(default=None, alias="errorMessage")


class _PiEvent(BaseModel):
    """One line of ``pi --mode json`` output."""

    model_config = ConfigDict(extra="ignore")

    type: str
    message: _PiMessage | None = None


def pi_completion_argv(
    *, provider_id: str, model: str, system_prompt_path: Path
) -> list[str]:
    """A tool-less, sessionless run with ``system_prompt_path``'s text as the whole system prompt and no project context."""
    return [
        "pi",
        "--mode",
        "json",
        "--no-session",
        "--no-tools",
        "--no-context-files",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-approve",
        "--thinking",
        "off",
        "--provider",
        provider_id,
        "--model",
        model,
        "--system-prompt",
        str(system_prompt_path),
    ]


def pi_task_argv(
    *, provider_id: str, model: str, instructions_path: Path | None
) -> list[str]:
    """A sessionless run with pi's default tools and the working directory's ``AGENTS.md``, plus any instructions file."""
    return [
        "pi",
        "--mode",
        "json",
        "--no-session",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-approve",
        "--provider",
        provider_id,
        "--model",
        model,
        *(
            ["--append-system-prompt", str(instructions_path)]
            if instructions_path is not None
            else []
        ),
    ]


def pi_environment(
    account_dir: Path, base_environment: Mapping[str, str]
) -> dict[str, str]:
    """``base_environment`` with pi bound to ``account_dir`` and no provider credential to run on instead."""
    environment = {
        name: value
        for name, value in base_environment.items()
        if name not in _STRIPPED_ENVIRONMENT_VARIABLES
        and not name.startswith(_CREDENTIAL_ENVIRONMENT_PREFIXES)
    }
    return {**environment, PI_AGENT_DIR_ENV_VAR: str(account_dir)}


def _decode_event(line: str) -> tuple[dict[str, object], _PiEvent]:
    try:
        decoded = json.loads(line)
    except json.JSONDecodeError as e:
        raise ProviderCallError(
            f"pi printed a line that is not an event: {line[:_LINE_DETAIL_CHARS]!r}"
        ) from e
    try:
        event = _PiEvent.model_validate(decoded)
    except ValidationError as e:
        raise ProviderCallError(
            f"pi printed a line that is not an event: {line[:_LINE_DETAIL_CHARS]!r}"
        ) from e
    return decoded, event


def _message_text(message: _PiMessage) -> str:
    if isinstance(message.content, str):
        return message.content
    return "\n".join(
        block.text
        for block in message.content
        if block.type == _TEXT_BLOCK and block.text
    )


def parse_pi_json_output(stdout: str) -> PiRunOutcome:
    """The last assistant message's text, with usage and cost summed over every assistant message pi printed."""
    decoded_events = [
        _decode_event(line) for line in stdout.splitlines() if line.strip()
    ]
    assistant_messages = [
        event.message
        for _, event in decoded_events
        if event.type == _MESSAGE_END_EVENT
        and event.message is not None
        and event.message.role == _ASSISTANT_ROLE
    ]
    if not assistant_messages:
        raise ProviderCallError("pi gave no answer")

    # The last message is the answer; an earlier failed attempt pi retried is not the run's failure
    last = assistant_messages[-1]
    if last.stop_reason in _FAILED_STOP_REASONS:
        raise ProviderCallError(
            f"pi's model call failed: {last.error_message or last.stop_reason}"
        )
    text = _message_text(last)
    if not text.strip():
        raise ProviderCallError("pi gave no answer")
    usage = Usage(
        input_tokens=sum(message.usage.input for message in assistant_messages),
        output_tokens=sum(message.usage.output for message in assistant_messages),
        cache_read_tokens=sum(
            message.usage.cache_read for message in assistant_messages
        ),
        cache_write_tokens=sum(
            message.usage.cache_write for message in assistant_messages
        ),
    )
    return PiRunOutcome(
        text=text,
        usage=usage,
        cost_usd=sum(message.usage.cost.total for message in assistant_messages),
        events=[decoded for decoded, _ in decoded_events],
    )


def run_pi(
    argv: Sequence[str],
    *,
    prompt_text: str,
    account_dir: Path,
    working_dir: Path,
    timeout_seconds: float,
) -> PiRunOutcome:
    """Run pi with the prompt on stdin, then closed, and parse its JSON events."""
    try:
        finished = run_in_own_process_group(
            argv,
            input_text=prompt_text,
            env=pi_environment(account_dir, os.environ),
            cwd=str(working_dir),
            timeout_seconds=timeout_seconds,
        )
    except subprocess.TimeoutExpired as e:
        raise ProviderTimeoutError(f"pi did not finish within {e.timeout:g}s") from e
    except OSError as e:
        raise ProviderCallError(f"could not start pi: {e}") from e
    if finished.returncode != 0:
        detail = finished.stderr.strip()[-_STDERR_DETAIL_CHARS:] or "no output"
        raise ProviderCallError(f"pi exited {finished.returncode}: {detail}")
    return parse_pi_json_output(finished.stdout)


def pi_complete(
    prompt: str,
    *,
    system: str,
    provider_id: str,
    model: str,
    account_dir: Path,
    timeout_seconds: float,
) -> PiRunOutcome:
    """One tool-less pi completion from an empty directory, with ``system`` as the whole system prompt."""
    with tempfile.TemporaryDirectory(prefix="pi_") as scratch:
        system_prompt_path = Path(scratch, _SYSTEM_PROMPT_FILENAME)
        system_prompt_path.write_text(system, encoding="utf-8")
        working_dir = Path(scratch, "cwd")
        working_dir.mkdir()
        return run_pi(
            pi_completion_argv(
                provider_id=provider_id,
                model=model,
                system_prompt_path=system_prompt_path,
            ),
            prompt_text=prompt,
            account_dir=account_dir,
            working_dir=working_dir,
            timeout_seconds=timeout_seconds,
        )


def pi_run_task(
    prompt: str,
    *,
    instructions: str | None,
    provider_id: str,
    model: str,
    account_dir: Path,
    working_dir: Path,
    timeout_seconds: float,
) -> PiRunOutcome:
    """One pi task with its default tools in ``working_dir``, ``instructions`` appended to its system prompt."""
    with tempfile.TemporaryDirectory(prefix="pi_") as scratch:
        instructions_path: Path | None = None
        if instructions is not None:
            instructions_path = Path(scratch, _INSTRUCTIONS_FILENAME)
            instructions_path.write_text(instructions, encoding="utf-8")
        return run_pi(
            pi_task_argv(
                provider_id=provider_id,
                model=model,
                instructions_path=instructions_path,
            ),
            prompt_text=prompt,
            account_dir=account_dir,
            working_dir=working_dir,
            timeout_seconds=timeout_seconds,
        )
