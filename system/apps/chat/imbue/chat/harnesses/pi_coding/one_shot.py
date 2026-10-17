"""Pi's one-shot completion: ``pi -p`` on the account's newest small model, under the account's pi config dir.

A pi account is a key for one provider, and providers name their models differently, so the
model is picked by family from what ``pi --list-models`` offers on the account's provider: the
newest Haiku, Luna or Flash model, with thinking off. An account offering none of them runs on
pi's own default model for the account, when that model is sure to be the account's.
"""

import os
import re
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from loguru import logger as _loguru_logger

from imbue.chat.harnesses.lanes import LANES
from imbue.chat.harnesses.one_shot import OneShotCompletion
from imbue.chat.harnesses.one_shot import OneShotCompletionError
from imbue.chat.harnesses.one_shot import OneShotCompletionTimeoutError
from imbue.chat.harnesses.one_shot import answer_on_first_model
from imbue.chat.harnesses.pi_coding.account_binding import PiAccountBinding
from imbue.chat.harnesses.pi_coding.model import list_pi_models
from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.imbue_common.pure import pure
from imbue.mngr.utils.file_utils import read_json_dict

logger = _loguru_logger

# Small, cheap model families, preferred in this order when a provider (OpenRouter, say) offers several.
_SMALL_MODEL_FAMILIES: Final[tuple[str, ...]] = ("haiku", "luna", "flash")
_TIMEOUT_SECONDS: Final = 60.0
_SLOW_WARNING_SECONDS: Final = 15.0
# Version numbers in model ids are small; anything longer is a date ("claude-haiku-4-5-20251001").
_MAX_VERSION_DIGITS: Final = 3

# pi reads a provider's key from its environment variable as well as from auth.json, so a key left
# in the server's environment would make that provider's models available to, and paid for by, a
# call that must run on the chat's account.
_PROVIDER_KEY_ENV_VARS: Final = frozenset(
    provider.env_var for lane in LANES for provider in lane.key_providers if provider.env_var
)


@pure
def _model_words(tag: str) -> tuple[str, ...]:
    """The words of a ``provider/model`` tag's model id ("openrouter/anthropic/claude-haiku-4.5" -> anthropic, claude, haiku, 4, 5)."""
    model_id = tag.partition("/")[2]
    return tuple(word for word in re.split(r"[-./:_]", model_id.lower()) if word)


@pure
def _newness(tag: str) -> tuple[tuple[int, ...], int]:
    """A sort key that puts the newest version last, and of one version the shortest id (the undated alias)."""
    version = tuple(int(word) for word in _model_words(tag) if word.isdigit() and len(word) <= _MAX_VERSION_DIGITS)
    return version, -len(tag)


@pure
def choose_pi_one_shot_models(
    offered_tags: tuple[str, ...], account_providers: frozenset[str]
) -> tuple[str | None, ...]:
    """The models to try in order: the newest small-family one on the account's providers, then None (the default).

    The default is left out unless every offered model is the account's own: pi picks it from whatever it can
    reach, so ambient cloud credentials (an ``~/.aws`` profile) would otherwise have it run on Amazon Bedrock.
    """
    own_tags = tuple(tag for tag in offered_tags if tag.partition("/")[0] in account_providers)
    small_model = _newest_small_model(own_tags)
    is_default_the_accounts = bool(own_tags) and len(own_tags) == len(offered_tags)
    return (
        *((small_model,) if small_model is not None else ()),
        *((None,) if is_default_the_accounts else ()),
    )


@pure
def _newest_small_model(tags: tuple[str, ...]) -> str | None:
    for family in _SMALL_MODEL_FAMILIES:
        family_tags = [tag for tag in tags if family in _model_words(tag)]
        if family_tags:
            return max(family_tags, key=_newness)
    return None


@pure
def pi_one_shot_argv(system_prompt: str, model: str | None) -> list[str]:
    """The ``pi -p`` argv, on ``model`` or the account's default when None. The prompt goes on stdin."""
    return [
        "pi",
        "-p",
        "--no-session",
        "--no-tools",
        # The account folder is a pi config dir; nothing it may load has a place in a one-line answer.
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-context-files",
        "--thinking",
        "off",
        *(["--model", model] if model is not None else []),
        "--system-prompt",
        system_prompt,
    ]


@pure
def pi_one_shot_env(ambient_env: Mapping[str, str], account_dir: Path) -> dict[str, str]:
    """The server's environment scoped to one account's pi config dir, without the server's own provider keys."""
    env = {key: value for key, value in ambient_env.items() if key not in _PROVIDER_KEY_ENV_VARS}
    env.update(PiAccountBinding().account_env(account_dir))
    return env


def _account_providers(account_dir: Path) -> frozenset[str]:
    """The providers the account holds a credential for: the keys of its ``auth.json``."""
    return frozenset(read_json_dict(PiAccountBinding().account_credential_path(account_dir)))


class PiOneShotCompletion(OneShotCompletion):
    """Runs ``pi -p`` from an empty directory with no tools, resources, or session kept."""

    def complete(self, account_dir: Path, system_prompt: str, prompt: str) -> str:
        """The answer on the first model in ``choose_pi_one_shot_models`` that gives one."""
        env = pi_one_shot_env(os.environ, account_dir)
        offered_tags = list_pi_models(env)
        if offered_tags is None:
            raise OneShotCompletionError(f"pi could not list the models of the account at {account_dir}")
        models = choose_pi_one_shot_models(offered_tags, _account_providers(account_dir))
        if not models:
            raise OneShotCompletionError(f"pi offers no model that runs only on the account at {account_dir}")
        return answer_on_first_model(models, lambda model: self._complete_on(model, env, system_prompt, prompt))

    def _complete_on(self, model: str | None, env: dict[str, str], system_prompt: str, prompt: str) -> str:
        started_at = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="chat_one_shot_") as isolated_dir:
            try:
                finished = run_local_command_modern_version(
                    command=pi_one_shot_argv(system_prompt, model),
                    is_checked=False,
                    timeout=_TIMEOUT_SECONDS,
                    cwd=Path(isolated_dir),
                    env=env,
                    name="pi one-shot completion",
                    stdin_bytes=prompt.encode("utf-8"),
                )
            except ProcessError as e:
                raise OneShotCompletionError(f"pi -p could not run: {e}") from e
        elapsed_seconds = time.monotonic() - started_at
        if finished.is_timed_out:
            raise OneShotCompletionTimeoutError(f"pi -p did not answer within {_TIMEOUT_SECONDS:.0f}s")
        if finished.returncode != 0:
            raise OneShotCompletionError(f"pi -p exited {finished.returncode}: {finished.stderr.strip()[-300:]}")
        if elapsed_seconds > _SLOW_WARNING_SECONDS:
            logger.warning("pi one-shot completion took {:.1f}s", elapsed_seconds)
        answer = finished.stdout.strip()
        if not answer:
            raise OneShotCompletionError("pi -p gave no answer")
        return answer
