"""Claude's one-shot completion: ``claude -p`` on the cheapest model the account answers on, under its config dir."""

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
from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.imbue_common.pure import pure

logger = _loguru_logger

# Cheapest first, as aliases the pinned Claude Code resolves (``baked_model_catalog_v2_1_280.json``)
# rather than dated ids, so each follows the CLI's own idea of the current model of its family. None
# is the account's default model, for an account that answers on neither.
ONE_SHOT_MODEL_PREFERENCE: Final[tuple[str | None, ...]] = ("haiku", "sonnet", None)
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
def claude_one_shot_env(ambient_env: Mapping[str, str], config_dir: Path) -> dict[str, str]:
    """The server's environment scoped to one account's config dir.

    The server's own auth keys are dropped, as the signed-in probe drops them: the call must be
    paid by the chat's account, not by a key left in the service's environment.
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


class _OneShotTimeoutError(OneShotCompletionError):
    """``claude -p`` did not answer in time; another model would wait as long, so none is tried."""


class ClaudeOneShotCompletion(OneShotCompletion):
    """Runs ``claude -p`` from an empty directory, so no project instructions or hooks reach the answer."""

    def complete(self, account_dir: Path, system_prompt: str, prompt: str) -> str:
        """The answer on the first model in ``ONE_SHOT_MODEL_PREFERENCE`` that gives one."""
        failures: list[str] = []
        for model in ONE_SHOT_MODEL_PREFERENCE:
            try:
                return self._complete_on(model, account_dir, system_prompt, prompt)
            except _OneShotTimeoutError:
                raise
            except OneShotCompletionError as e:
                failures.append(f"{model or 'default model'}: {e}")
        raise OneShotCompletionError("; ".join(failures))

    def _complete_on(self, model: str | None, account_dir: Path, system_prompt: str, prompt: str) -> str:
        env = claude_one_shot_env(os.environ, account_dir)
        started_at = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="chat_one_shot_") as isolated_dir:
            try:
                finished = run_local_command_modern_version(
                    command=claude_one_shot_argv(system_prompt, model),
                    is_checked=False,
                    timeout=_TIMEOUT_SECONDS,
                    cwd=Path(isolated_dir),
                    env=env,
                    name="claude one-shot completion",
                    stdin_bytes=prompt.encode("utf-8"),
                )
            except ProcessError as e:
                raise OneShotCompletionError(f"claude -p could not run: {e}") from e
        elapsed_seconds = time.monotonic() - started_at
        if finished.is_timed_out:
            raise _OneShotTimeoutError(f"claude -p did not answer within {_TIMEOUT_SECONDS:.0f}s")
        if finished.returncode != 0:
            raise OneShotCompletionError(f"claude -p exited {finished.returncode}: {finished.stderr.strip()[:300]}")
        if elapsed_seconds > _SLOW_WARNING_SECONDS:
            logger.warning("claude one-shot completion took {:.1f}s", elapsed_seconds)
        return parse_claude_print_result(finished.stdout)
