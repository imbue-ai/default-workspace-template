"""Codex's one-shot completion: ``codex exec`` on the account's cheapest model, bound to the account folder.

The model comes from the account's own model list, not a fixed id: model lists differ by
subscription and change between releases. The newest "luna" model the account offers is used, at
its lowest reasoning effort; an account offering none runs on its default model.
"""

import re
import tempfile
import time
from pathlib import Path
from typing import Final

from loguru import logger as _loguru_logger
from pydantic import Field

from imbue.chat.harnesses.codex.account_models import AccountModelProbeError
from imbue.chat.harnesses.codex.account_models import codex_account_environment
from imbue.chat.harnesses.codex.account_models import probe_codex_account_models
from imbue.chat.harnesses.codex.model import codex_model_options_path
from imbue.chat.harnesses.codex.model import read_codex_model_options
from imbue.chat.harnesses.codex.model import write_codex_model_options
from imbue.chat.harnesses.one_shot import OneShotCompletion
from imbue.chat.harnesses.one_shot import OneShotCompletionError
from imbue.chat.harnesses.one_shot import OneShotCompletionTimeoutError
from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr_codex.app_server_client import CodexModel

logger = _loguru_logger

# The family of OpenAI's small, cheap model across versions (gpt-5.6-luna, gpt-6-luna).
_CHEAP_MODEL_FAMILY: Final = "luna"
# Cheapest first; an effort outside this list is never chosen.
_EFFORT_LADDER: Final[tuple[str, ...]] = ("none", "minimal", "low", "medium", "high", "xhigh")
_TIMEOUT_SECONDS: Final = 60.0
_SLOW_WARNING_SECONDS: Final = 15.0
_ANSWER_FILENAME: Final = "answer.txt"


class CodexOneShotChoice(FrozenModel):
    """The model and effort a one-shot call runs on; None for either leaves the account's default."""

    model: str | None = Field(default=None, description="The model id, or None for the account's default model")
    effort: str | None = Field(default=None, description="The reasoning effort, or None for the model's default")


@pure
def _version_of(model_id: str) -> tuple[int, ...]:
    """The dotted version in a model id ("gpt-5.6-luna" -> (5, 6)); () when it has none."""
    for part in model_id.split("-"):
        if re.fullmatch(r"\d+(\.\d+)*", part):
            return tuple(int(number) for number in part.split("."))
    return ()


@pure
def _cheapest_effort(model: CodexModel) -> str | None:
    offered = {option.reasoning_effort for option in model.supported_reasoning_efforts}
    return next((effort for effort in _EFFORT_LADDER if effort in offered), None)


@pure
def choose_codex_one_shot_model(models: tuple[CodexModel, ...]) -> CodexOneShotChoice:
    """The newest "luna" model in ``models`` at its lowest effort; else the default model at its lowest effort."""
    cheap_models = [model for model in models if _CHEAP_MODEL_FAMILY in model.model.lower().split("-")]
    if cheap_models:
        newest = max(cheap_models, key=lambda model: _version_of(model.model))
        return CodexOneShotChoice(model=newest.model, effort=_cheapest_effort(newest))
    default_model = next((model for model in models if model.is_default), None)
    return CodexOneShotChoice(effort=None if default_model is None else _cheapest_effort(default_model))


@pure
def codex_one_shot_argv(choice: CodexOneShotChoice, answer_path: Path) -> list[str]:
    """The ``codex exec`` argv. The prompt goes on stdin (``-``), so a message starting with ``-`` is never a flag."""
    return [
        "codex",
        "exec",
        *(["--model", choice.model] if choice.model is not None else []),
        *(["-c", f'model_reasoning_effort="{choice.effort}"'] if choice.effort is not None else []),
        # The account folder is a CODEX_HOME; its config (MCP servers, hooks) has no place in a
        # one-line answer, and its auth is still read.
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--color",
        "never",
        "--output-last-message",
        str(answer_path),
        "-",
    ]


def _account_models(account_dir: Path) -> tuple[CodexModel, ...]:
    """The models the account offers: what was last read of it, else a fresh read (kept for next time)."""
    options_path = codex_model_options_path(account_dir)
    models = read_codex_model_options(options_path)
    if models:
        return models
    try:
        models = probe_codex_account_models(account_dir)
    except AccountModelProbeError as e:
        logger.debug("Could not read the models of the codex account at {}: {}", account_dir, e)
        return ()
    if models:
        write_codex_model_options(options_path, models)
    return models


class CodexOneShotCompletion(OneShotCompletion):
    """Runs ``codex exec`` from an empty directory with no tools that can write, and no session kept."""

    def complete(self, account_dir: Path, system_prompt: str, prompt: str) -> str:
        """The answer on the account's cheapest model; on the default model when that one is refused."""
        choice = choose_codex_one_shot_model(_account_models(account_dir))
        # codex exec takes no system prompt of its own, so the instructions lead the message.
        full_prompt = f"{system_prompt}\n\nThe message:\n\n{prompt}"
        try:
            return self._complete_on(choice, account_dir, full_prompt)
        except OneShotCompletionTimeoutError:
            raise
        except OneShotCompletionError as e:
            if choice.model is None:
                raise
            # The model list read earlier may name a model the account no longer offers.
            logger.info("codex one-shot on {} failed, using the default model: {}", choice.model, e)
            return self._complete_on(CodexOneShotChoice(), account_dir, full_prompt)

    def _complete_on(self, choice: CodexOneShotChoice, account_dir: Path, prompt: str) -> str:
        started_at = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="chat_one_shot_") as isolated_dir:
            answer_path = Path(isolated_dir) / _ANSWER_FILENAME
            try:
                finished = run_local_command_modern_version(
                    command=codex_one_shot_argv(choice, answer_path),
                    is_checked=False,
                    timeout=_TIMEOUT_SECONDS,
                    cwd=Path(isolated_dir),
                    env=codex_account_environment(account_dir),
                    name="codex one-shot completion",
                    stdin_bytes=prompt.encode("utf-8"),
                )
            except ProcessError as e:
                raise OneShotCompletionError(f"codex exec could not run: {e}") from e
            if finished.is_timed_out:
                raise OneShotCompletionTimeoutError(f"codex exec did not answer within {_TIMEOUT_SECONDS:.0f}s")
            if finished.returncode != 0:
                raise OneShotCompletionError(
                    f"codex exec exited {finished.returncode}: {finished.stderr.strip()[-300:]}"
                )
            answer = answer_path.read_text(encoding="utf-8").strip() if answer_path.is_file() else ""
        elapsed_seconds = time.monotonic() - started_at
        if elapsed_seconds > _SLOW_WARNING_SECONDS:
            logger.warning("codex one-shot completion took {:.1f}s", elapsed_seconds)
        if not answer:
            raise OneShotCompletionError("codex exec gave no answer")
        return answer
