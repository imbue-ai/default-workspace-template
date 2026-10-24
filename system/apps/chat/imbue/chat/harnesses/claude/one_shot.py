"""Claude's one-shot completion: ``claude -p`` on the cheapest model the account answers on, under its config dir.

Each run logs under a session id chosen up front; once it exits, its calls are read from that
session log into the workspace's usage and the log is deleted, so a one-shot is counted
without leaving a session behind under the account.
"""

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
from workspace_usage.claude_one_shot import claude_session_args
from workspace_usage.claude_one_shot import new_claude_session_id
from workspace_usage.claude_one_shot import record_claude_one_shot

from imbue.chat.harnesses.claude.account_binding import ClaudeAccountBinding
from imbue.chat.harnesses.claude.auth import MANAGED_AUTH_ENV_KEYS
from imbue.chat.harnesses.one_shot import OneShotCompletion
from imbue.chat.harnesses.one_shot import OneShotCompletionError
from imbue.chat.harnesses.one_shot import OneShotCompletionTimeoutError
from imbue.chat.harnesses.one_shot import OneShotUsage
from imbue.chat.harnesses.one_shot import answer_on_first_model
from imbue.chat.harnesses.one_shot import describe_model_name
from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.imbue_common.pure import pure

logger = _loguru_logger

# The haiku alias the pinned Claude Code resolves rather than a dated id, so it follows the CLI's own
# idea of the current Haiku; then None, the account's default model, for an account that refuses it.
ONE_SHOT_MODEL_PREFERENCE: Final[tuple[str | None, ...]] = ("haiku", None)
_TIMEOUT_SECONDS: Final = 60.0
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
def claude_one_shot_argv(system_prompt: str, model: str | None, session_id: str) -> list[str]:
    """The ``claude -p`` argv, on ``model`` or the account's default when None, logging under ``session_id``.

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
        # The session log is how the call's usage is recorded; the usage collector deletes it once read.
        *claude_session_args(session_id),
    ]


@pure
def claude_one_shot_env(ambient_env: Mapping[str, str], config_dir: Path) -> dict[str, str]:
    """The server's environment scoped to one account's config dir.

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
    # on Haiku, about 3s with thinking against under 1s without, for the same name.
    env["MAX_THINKING_TOKENS"] = "0"
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

    def complete(self, account_dir: Path, system_prompt: str, prompt: str, usage: OneShotUsage | None = None) -> str:
        """The answer on the first model in ``ONE_SHOT_MODEL_PREFERENCE`` that gives one."""
        return answer_on_first_model(
            ONE_SHOT_MODEL_PREFERENCE,
            lambda model: self._complete_on(model, account_dir, system_prompt, prompt, usage),
            describe_model_name,
        )

    def _complete_on(
        self, model: str | None, account_dir: Path, system_prompt: str, prompt: str, usage: OneShotUsage | None
    ) -> str:
        env = claude_one_shot_env(os.environ, account_dir)
        session_id = new_claude_session_id()
        started_at = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="chat_one_shot_") as isolated_dir:
            try:
                finished = run_local_command_modern_version(
                    command=claude_one_shot_argv(system_prompt, model, session_id),
                    is_checked=False,
                    timeout=_TIMEOUT_SECONDS,
                    cwd=Path(isolated_dir),
                    env=env,
                    name="claude one-shot completion",
                    stdin_bytes=prompt.encode("utf-8"),
                )
            except ProcessError as e:
                raise OneShotCompletionError(f"claude -p could not run: {e}") from e
            finally:
                # A call that failed or timed out may still have used tokens, so it is recorded too.
                record_claude_one_shot(
                    account_dir,
                    session_id,
                    purpose=usage.purpose if usage is not None else None,
                    context=usage.context if usage is not None else None,
                )
        elapsed_seconds = time.monotonic() - started_at
        if finished.is_timed_out:
            raise OneShotCompletionTimeoutError(f"claude -p did not answer within {_TIMEOUT_SECONDS:.0f}s")
        if finished.returncode != 0:
            raise OneShotCompletionError(f"claude -p exited {finished.returncode}: {finished.stderr.strip()[:300]}")
        if elapsed_seconds > _SLOW_WARNING_SECONDS:
            logger.warning("claude one-shot completion took {:.1f}s", elapsed_seconds)
        return parse_claude_print_result(finished.stdout)
