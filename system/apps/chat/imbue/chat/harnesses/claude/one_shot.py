"""Claude's one-shot completion: ``claude -p`` on the model of the asked size the account answers on, under its
config dir."""

import json
import os
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from loguru import logger as _loguru_logger
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import ValidationError

from imbue.chat.harnesses.claude.account_binding import ClaudeAccountBinding
from imbue.chat.harnesses.claude.auth import MANAGED_AUTH_ENV_KEYS
from imbue.chat.harnesses.one_shot import OneShotCompletion
from imbue.chat.harnesses.one_shot import OneShotCompletionError
from imbue.chat.harnesses.one_shot import OneShotCompletionTimeoutError
from imbue.chat.harnesses.one_shot import OneShotModelTier
from imbue.chat.harnesses.one_shot import answer_on_first_model
from imbue.chat.harnesses.one_shot import describe_model_name
from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.imbue_common.pure import pure

logger = _loguru_logger

# For each tier, the alias the pinned Claude Code resolves rather than a dated id, so it follows the CLI's own
# idea of the current model of that size; then None, the account's default model, for an account that refuses it.
ONE_SHOT_MODEL_PREFERENCE_BY_TIER: Final[Mapping[OneShotModelTier, tuple[str | None, ...]]] = {
    OneShotModelTier.SMALL: ("haiku", None),
    OneShotModelTier.MEDIUM: ("sonnet", None),
}
_SLOW_WARNING_SECONDS: Final = 15.0

# An inherited MAIN_CLAUDE_SESSION_ID makes the child look like mngr's managed main session
# and trips its hooks; the mngr identity vars go with it for the same reason.
_SESSION_ENV_KEYS: Final = frozenset(
    ("MAIN_CLAUDE_SESSION_ID", "MNGR_AGENT_STATE_DIR", "MNGR_AGENT_NAME", "MNGR_HOST_DIR")
)


class _ClaudePrintResult(BaseModel):
    """The fields of a ``claude -p --output-format json`` result this reads."""

    model_config = ConfigDict(extra="ignore")

    subtype: str | None = None
    is_error: bool = False
    result: str | None = None


@pure
def claude_one_shot_argv(system_prompt: str, model: str | None) -> list[str]:
    """The ``claude -p`` argv, on ``model`` or the account's default when None.

    The prompt goes on stdin, so a message starting with ``-`` is never read as a flag.
    """
    return [
        "claude",
        "-p",
        "--output-format",
        "json",
        *(["--model", model] if model is not None else []),
        "--system-prompt",
        system_prompt,
        "--tools",
        "",
        # A persisted session would file a projects/ directory under the account for every call.
        "--no-session-persistence",
    ]


@pure
def claude_one_shot_env(
    ambient_env: Mapping[str, str], config_dir: Path, thinking_token_allowance: int | None
) -> dict[str, str]:
    """The server's environment scoped to one account's config dir, with thinking capped at the allowance.

    The server's own auth keys are dropped: the call must be paid by the chat's account, not by a
    key left in the service's environment.
    """
    env = {
        key: value
        for key, value in ambient_env.items()
        if key not in MANAGED_AUTH_ENV_KEYS and key not in _SESSION_ENV_KEYS
    }
    env.update(ClaudeAccountBinding().account_env(config_dir))
    # An account that thinks by default spends most of a short answer's time thinking: measured
    # on Haiku, about 3s with thinking against under 1s without, for the same name. So thinking is
    # off unless the caller allows some. A model that decides for itself how much to think is asked
    # the same way whatever this says.
    env["MAX_THINKING_TOKENS"] = str(0 if thinking_token_allowance is None else thinking_token_allowance)
    return env


@pure
def parse_claude_print_result(stdout: str) -> str:
    """The answer text of a ``claude -p`` JSON result. Raises ``OneShotCompletionError`` for an error or malformed one."""
    try:
        parsed = _ClaudePrintResult.model_validate(json.loads(stdout))
    except (ValueError, ValidationError) as e:
        raise OneShotCompletionError(f"claude -p printed no readable result: {e}") from e
    if parsed.is_error or parsed.subtype != "success" or parsed.result is None:
        raise OneShotCompletionError(f"claude -p returned no answer (subtype={parsed.subtype!r})")
    return parsed.result


class ClaudeOneShotCompletion(OneShotCompletion):
    """Runs ``claude -p`` from an empty directory, so no project instructions or hooks reach the answer."""

    def complete(
        self,
        account_dir: Path,
        system_prompt: str,
        prompt: str,
        *,
        model_tier: OneShotModelTier,
        thinking_token_allowance: int | None,
        timeout_seconds: float,
    ) -> str:
        """The answer on the first model of the tier's preference list that gives one."""
        env = claude_one_shot_env(os.environ, account_dir, thinking_token_allowance)
        return answer_on_first_model(
            ONE_SHOT_MODEL_PREFERENCE_BY_TIER[model_tier],
            lambda model: self._complete_on(model, env, system_prompt, prompt, timeout_seconds),
            describe_model_name,
        )

    def _complete_on(
        self, model: str | None, env: Mapping[str, str], system_prompt: str, prompt: str, timeout_seconds: float
    ) -> str:
        started_at = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="chat_one_shot_") as isolated_dir:
            try:
                finished = run_local_command_modern_version(
                    command=claude_one_shot_argv(system_prompt, model),
                    is_checked=False,
                    timeout=timeout_seconds,
                    cwd=Path(isolated_dir),
                    env=env,
                    name="claude one-shot completion",
                    stdin_bytes=prompt.encode("utf-8"),
                )
            except ProcessError as e:
                raise OneShotCompletionError(f"claude -p could not run: {e}") from e
        elapsed_seconds = time.monotonic() - started_at
        if finished.is_timed_out:
            raise OneShotCompletionTimeoutError(f"claude -p did not answer within {timeout_seconds:.0f}s")
        if finished.returncode != 0:
            raise OneShotCompletionError(f"claude -p exited {finished.returncode}: {finished.stderr.strip()[:300]}")
        if elapsed_seconds > _SLOW_WARNING_SECONDS:
            logger.warning("claude one-shot completion took {:.1f}s", elapsed_seconds)
        return parse_claude_print_result(finished.stdout)
