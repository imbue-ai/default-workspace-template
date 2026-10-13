"""Claude's harness session: the file-harness session, plus a gateway account's model set."""

import threading

from imbue.chat.agent_discovery import read_claude_config_dir_from_env_file
from imbue.chat.harnesses.claude.gateway_models import cached_gateway_options
from imbue.chat.harnesses.claude.model import CLAUDE_EFFORTS
from imbue.chat.harnesses.model import ModelOption
from imbue.chat.harnesses.sending_registry import SendingRegistry
from imbue.chat.harnesses.session import FileHarnessSession
from imbue.chat.harnesses.session import SessionDeps


class ClaudeHarnessSession(FileHarnessSession):
    """A claude agent on a gateway account matches and validates against the gateway's models.

    Everything else is the file-harness session's. An agent on Anthropic itself keeps the static
    catalog; one whose account points claude at a gateway (Concentrate AI) is offered the gateway's
    list by the picker, so the chip has to match against that same list or a model picked from it
    would read as unrecognized, and the switch would reject it as unknown.
    """

    _offered_options: tuple[ModelOption, ...] | None

    @classmethod
    def build(cls, deps: SessionDeps) -> "ClaudeHarnessSession":
        # The base's fields, set here so the subclass is the static type too (as agy's session does).
        self = cls.__new__(cls)
        self._deps = deps
        self._sending = SendingRegistry.build()
        self._sending_lock = threading.Lock()
        self._offered_options = None
        return self

    def switch_options(self) -> tuple[ModelOption, ...]:
        """The last picker-open's set; before one, the list last kept for the account; else the catalog.

        The kept list is what lets the chip name a non-Claude model after a restart, before anyone
        has opened the picker again.
        """
        if self._offered_options:
            return self._offered_options
        catalog_options = self._deps.catalog_options()
        account_dir = read_claude_config_dir_from_env_file(self._deps.state_dir)
        kept = cached_gateway_options(account_dir, catalog_options, CLAUDE_EFFORTS)
        if kept:
            self._offered_options = kept
            return kept
        return catalog_options

    def note_offered_options(self, options: tuple[ModelOption, ...]) -> None:
        self._offered_options = options
