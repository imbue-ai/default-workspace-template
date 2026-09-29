"""Asking a harness one question on a chat's own account, outside the chat.

The chat app sometimes needs a small model answer about a chat -- its name, for one -- that
must not land in the chat's transcript or spend its context. A harness that can answer one
prompt headlessly registers a ``OneShotCompletion`` on its ``HarnessSpec``; the answer is
paid for by the account the chat already runs on, since a workspace has no model access of
its own.
"""

from abc import ABC
from abc import abstractmethod
from pathlib import Path


class OneShotCompletionError(RuntimeError):
    """A one-shot completion could not be run or gave no usable answer."""


class OneShotCompletion(ABC):
    """Answers one prompt with the harness's smallest model on an account, with no tools and no session left behind."""

    @abstractmethod
    def complete(self, account_dir: Path, system_prompt: str, prompt: str) -> str:
        """The model's answer text. Raises ``OneShotCompletionError`` when the call fails."""
