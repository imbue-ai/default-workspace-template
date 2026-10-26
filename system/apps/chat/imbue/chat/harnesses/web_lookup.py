"""Asking a harness to search the public web on a chat's own account, outside the chat.

The onboarding agent's look-up of the user must not land in the chat's transcript, spend its
context, or reach anything on this machine. A harness that can run one headless search registers a
``WebLookup`` on its ``HarnessSpec``. The search runs on the model the chat is set to and is paid for
by the account the chat already runs on, since a workspace has no model access of its own. It gets the
web tools and nothing else, from an empty directory, so a page's instructions reach no file or shell.
"""

from abc import ABC
from abc import abstractmethod
from pathlib import Path


class WebLookupError(RuntimeError):
    """A web look-up could not be run or gave no usable answer."""


class WebLookupTimeoutError(WebLookupError):
    """The harness's CLI did not answer in time."""


class WebLookup(ABC):
    """Runs one web search for a prompt on an account's model, with no other tools and no session left behind."""

    @abstractmethod
    def search(self, account_dir: Path, system_prompt: str, prompt: str, model: str | None) -> str:
        """The model's answer text, on ``model`` or the account's own default when None.

        Raises ``WebLookupError`` when the call fails.
        """
