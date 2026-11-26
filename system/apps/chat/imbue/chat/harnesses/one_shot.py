"""Asking a harness one question on a chat's own account, outside the chat.

The chat app sometimes needs a model's answer about a chat -- its name, the replies to suggest
under its last message -- that must not land in the chat's transcript or spend its context. A
harness that can answer one prompt headlessly registers a ``OneShotCompletion`` on its
``HarnessSpec``; the answer is paid for by the account the chat already runs on, since a
workspace has no model access of its own.
"""

from abc import ABC
from abc import abstractmethod
from collections.abc import Callable
from collections.abc import Sequence
from enum import auto
from pathlib import Path
from typing import TypeVar

from loguru import logger as _loguru_logger

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.pure import pure

logger = _loguru_logger


class OneShotCompletionError(RuntimeError):
    """A one-shot completion could not be run or gave no usable answer."""


class OneShotCompletionTimeoutError(OneShotCompletionError):
    """The harness's CLI did not answer in time; another model would wait as long, so no fallback is tried."""


_ModelT = TypeVar("_ModelT")


@pure
def describe_model_name(model: str | None) -> str:
    """A model named by its id, None meaning the account's default, as a failure names it."""
    return model or "default model"


def answer_on_first_model(
    models: Sequence[_ModelT],
    complete_on: Callable[[_ModelT], str],
    describe: Callable[[_ModelT], str],
) -> str:
    """``complete_on``'s answer on the first of ``models`` that gives one.

    Raises ``OneShotCompletionError`` naming every model's failure (by ``describe``), or the first timeout as it is.
    """
    failures: list[str] = []
    for model in models:
        try:
            return complete_on(model)
        except OneShotCompletionTimeoutError:
            raise
        except OneShotCompletionError as e:
            logger.info("One-shot completion on {} failed: {}", describe(model), e)
            failures.append(f"{describe(model)}: {e}")
    raise OneShotCompletionError("; ".join(failures))


class OneShotModelTier(LowerCaseStrEnum):
    """How large a model a question is asked of. Each harness names its own model for a tier."""

    # The harness's smallest model: a one-line answer about one message.
    SMALL = auto()
    # A mid-sized model, smaller than the one a chat usually runs on: a judgement over a stretch of conversation.
    MEDIUM = auto()


class OneShotCompletion(ABC):
    """Answers one prompt on an account with a model of the size asked for, with no tools and no session left behind."""

    @abstractmethod
    def complete(
        self,
        account_dir: Path,
        system_prompt: str,
        prompt: str,
        *,
        model_tier: OneShotModelTier,
        # How many tokens the model may think for before it answers; None for an answer with no thinking.
        thinking_token_allowance: int | None,
        # How long the harness's CLI may take over one model before the call gives up on it.
        timeout_seconds: float,
    ) -> str:
        """The model's answer text. Raises ``OneShotCompletionError`` when the call fails, and its
        ``OneShotCompletionTimeoutError`` when the CLI did not answer within ``timeout_seconds``."""
