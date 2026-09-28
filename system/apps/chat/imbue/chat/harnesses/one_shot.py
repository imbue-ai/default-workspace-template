"""Asking a harness one question on a chat's own account, outside the chat.

The chat app sometimes needs a small model answer about a chat -- its name, for one -- that
must not land in the chat's transcript or spend its context. A harness that can answer one
prompt headlessly registers a ``OneShotCompletion`` on its ``HarnessSpec``; the answer is
paid for by the account the chat already runs on, since a workspace has no model access of
its own.
"""

from abc import ABC
from abc import abstractmethod

from imbue.chat.agent_discovery import AgentInfo


class OneShotCompletionError(RuntimeError):
    """A one-shot completion could not be run or gave no usable answer."""


class OneShotCompletion(ABC):
    """Answers one prompt with a small model on the account an agent runs on, with no tools and no session left behind."""

    @abstractmethod
    def complete(self, agent_info: AgentInfo, system_prompt: str, prompt: str) -> str:
        """The model's answer text. Raises ``OneShotCompletionError`` when the call fails."""
