"""Moving a chat from one harness to another, without ending the conversation.

The whole feature is one ordered sequence with a single commit point, and the
order is the design:

    check -> freeze -> archive -> write the handover -> create the replacement
          -> [COMMIT: re-point the chat] -> unhide, destroy the old agent

Everything before the commit is reversible: the outgoing agent is frozen but
intact, so a failed candidate is destroyed, the freeze is lifted, and the user is
back where they started with their history untouched. Nothing after it is
reversible, and nothing after it can fail in a way that loses the conversation --
the archive is already on disk and the registry already names the successor.

The freeze comes first for a reason that is not about tidiness: between the
archive and the handover there is a window in which the outgoing agent would
still accept a turn, from our own UI or from ``mngr message`` in a terminal. A
turn taken in that window is a turn the successor never learns about and the user
never sees again. The freeze closes it system-wide, because it lives on the agent
in mngr rather than in this process.
"""

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any
from typing import NoReturn

from loguru import logger as _loguru_logger
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.primitives import AgentId
from imbue.system_interface.accounts import AccountError
from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.agent_manager import AgentManager
from imbue.system_interface.handoff_archive import TranscriptArchive
from imbue.system_interface.handoff_context import build_handoff_first_message
from imbue.system_interface.handoff_context import handoff_dir_for_workspace
from imbue.system_interface.handoff_context import write_handoff_context
from imbue.system_interface.harnesses.binding import BindingError
from imbue.system_interface.harnesses.binding import harness_for
from imbue.system_interface.harnesses.binding import resolve_binding
from imbue.system_interface.harnesses.harness_type import HarnessType
from imbue.system_interface.harnesses.session_watcher import AgentSessionWatcher
from imbue.system_interface.models import AgentCreationError
from imbue.system_interface.models import ChatId
from imbue.system_interface.models import HandoffPhase
from imbue.system_interface.models import HandoffState

_FIRST_MESSAGE_FILENAME = "first_message.md"


class HandoffError(RuntimeError):
    """A harness switch that did not happen, with the HTTP status that says why.

    Carries the status because the distinction the caller has to make is exactly
    the one the user sees: 404 (no such chat), 409 (the chat cannot be switched
    right now, and trying again later may work), 500 (the switch was attempted
    and broke). Wording is user-facing -- the endpoint returns ``str(error)``.
    """

    def __init__(self, message: str, http_status: int) -> None:
        super().__init__(message)
        self.http_status = http_status


class HandoffCoordinator(MutableModel):
    """Runs harness switches, one at a time per chat.

    Holds no state a restart needs to recover: an interrupted switch leaves at
    most a frozen agent and an orphaned candidate, both of which are visible on
    the machine and neither of which can have taken a turn. That is a deliberate
    trade -- a durable operation log would let us resume, but resuming a
    half-created agent is more machinery than destroying it and letting the user
    press the button again.
    """

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    agent_manager: AgentManager
    transcript_archive: TranscriptArchive
    # The app's watcher registry, injected as its two functions rather than as the
    # state object that owns it: this module would otherwise have to import
    # ``SystemInterfaceState``, which imports the agent manager and the archive that
    # are already collaborators here.
    get_watcher: Callable[[AgentInfo], AgentSessionWatcher]
    evict_watcher: Callable[[str], None]

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    # Chats with a switch in flight, and the operation that owns each. Duplicate
    # confirms are common (two windows, a double click, a retried request), and a
    # second switch of one chat must not run beside the first.
    _operation_id_by_chat_id: dict[ChatId, str] = PrivateAttr(default_factory=dict)
    # Owns the switch threads. Entered and exited manually because its lifetime is the
    # coordinator's, not one call's -- the repo's idiom for a long-lived group (see
    # ``AgentManager``'s creation and observe groups).
    _concurrency_group: ConcurrencyGroup = PrivateAttr(
        default_factory=lambda: ConcurrencyGroup(name="harness-handoff")
    )

    def model_post_init(self, context: object, /) -> None:
        self._concurrency_group.__enter__()

    def close(self) -> None:
        """Release the switch threads. Called from the app's shutdown."""
        self._concurrency_group.__exit__(None, None, None)

    def _claim(self, chat_id: ChatId, operation_id: str) -> bool:
        """Take the chat's switch slot for ``operation_id``. False if it is already claimed.

        The same operation id re-claiming its own slot is NOT a claim: the first
        request is still running it, so the retry has nothing to do but wait for
        the state the first one is publishing.
        """
        with self._lock:
            if chat_id in self._operation_id_by_chat_id:
                return False
            self._operation_id_by_chat_id[chat_id] = operation_id
            return True

    def _release(self, chat_id: ChatId) -> None:
        with self._lock:
            self._operation_id_by_chat_id.pop(chat_id, None)

    def start_switch(self, chat_id: ChatId, account_id: str, operation_id: str) -> None:
        """Check that ``chat_id`` can move onto ``account_id``'s harness, then start doing it.

        Every reason to refuse is checked HERE, on the request thread, so the user
        gets it as the answer to their click rather than as an error that arrives
        later out of nowhere. The switch itself then runs on its own thread and is
        reported through ``AgentStateItem.handoff``: bringing up a replacement
        agent takes as long as a cold harness start plus a first turn, which is far
        past the point where an HTTP request is the right place to be waiting.

        Raises ``HandoffError`` for a refusal, leaving nothing started.
        """
        registry = self.agent_manager.chat_registry
        record = registry.get(chat_id)
        if record is None:
            raise HandoffError(f"No chat {chat_id}", 404)
        old_agent_id = record.active_agent_id
        old_agent = self.agent_manager.get_agent_by_id(old_agent_id)
        if old_agent is None:
            raise HandoffError("This chat's agent is not running, so it cannot be switched", 409)

        target_harness = self._resolve_target_harness(account_id)
        if target_harness == old_agent.harness:
            raise HandoffError(f"This chat already runs on {target_harness.value}", 409)
        if old_agent.activity_state != ActivityState.IDLE:
            raise HandoffError("Wait for the current turn to finish before switching harness", 409)
        if old_agent.queued_messages:
            raise HandoffError("Send or clear the queued messages before switching harness", 409)

        if not self._claim(chat_id, operation_id):
            raise HandoffError("This chat is already switching harness", 409)
        self._concurrency_group.start_new_thread(
            target=self._run_switch_and_release,
            kwargs={
                "chat_id": chat_id,
                "old_agent_id": old_agent_id,
                "old_harness": old_agent.harness,
                "old_agent_name": old_agent.name,
                "display_name": old_agent.labels.get("display_name") or old_agent.name,
                "project_id": old_agent.labels.get("project", ""),
                "account_id": account_id,
                "target_harness": target_harness,
                "operation_id": operation_id,
            },
            name=f"switch-{chat_id[:8]}",
            is_checked=False,
        )

    def _run_switch_and_release(self, **kwargs: Any) -> None:
        """Run one switch off the request thread, always freeing the chat's slot.

        The catch-all is the point of the wrapper: the thread is unchecked, so an
        exception escaping here would be swallowed and the chat would be left
        wedged -- its slot claimed forever, its progress stuck mid-switch, and no
        way for the user to try again. Anything unexpected is published as a
        failure instead, which at worst tells them the truth.
        """
        chat_id: ChatId = kwargs["chat_id"]
        try:
            self._run_switch(**kwargs)
        except HandoffError as e:
            _loguru_logger.warning("Harness switch of chat {} did not complete: {}", chat_id, e)
        except Exception as e:
            _loguru_logger.opt(exception=e).error("Unexpected failure switching the harness of chat {}", chat_id)
            self.agent_manager.set_handoff_state(
                chat_id,
                HandoffState(
                    phase=HandoffPhase.FAILED,
                    target_harness=kwargs["target_harness"],
                    detail=f"Unexpected {type(e).__name__}: {e}",
                ),
            )
        finally:
            self._release(chat_id)

    def _resolve_target_harness(self, account_id: str) -> HarnessType:
        """The harness the chat is being moved onto, from the account it is being bound to.

        The account decides, exactly as it does at creation: a harness is not
        something the user picks independently of a credential to run it on. Which
        is why this goes through the same ``resolve_binding`` the create path uses
        rather than reading the account itself -- an account this build has no lane
        for must be refused here, before anything is frozen, not discovered when
        the replacement create rejects it.
        """
        try:
            account = resolve_binding(account_id)
        except (AccountError, BindingError) as e:
            raise HandoffError(str(e), 409) from e
        harness = harness_for(account)
        if harness is None:
            raise HandoffError(f"Account {account_id} is not bound to a known harness", 409)
        return harness

    def _run_switch(
        self,
        *,
        chat_id: ChatId,
        old_agent_id: str,
        old_harness: HarnessType,
        old_agent_name: str,
        display_name: str,
        project_id: str,
        account_id: str,
        target_harness: HarnessType,
        operation_id: str,
    ) -> str:
        manager = self.agent_manager
        manager.set_handoff_state(
            chat_id,
            HandoffState(phase=HandoffPhase.PREPARING, target_harness=target_harness, detail="Freezing the chat"),
        )

        freeze_failure = manager.set_chat_frozen(old_agent_id, operation_id)
        if freeze_failure is not None:
            self._fail(chat_id, target_harness, f"Could not hold the chat still: {freeze_failure}")

        agent_info = manager.get_agent_info_by_id(old_agent_id)
        if agent_info is None:
            manager.set_chat_frozen(old_agent_id, "")
            self._fail(chat_id, target_harness, "This chat's agent went away before it could be switched")

        # Archive first, and eagerly: ``mngr destroy`` takes the outgoing agent's
        # transcript files with it, so anything not copied out now is gone for good.
        watcher = self.get_watcher(agent_info)
        events = watcher.get_all_events()
        archived_count = self.transcript_archive.capture(chat_id, old_agent_id, watcher)
        _loguru_logger.info("Archived {} events of chat {} before its harness switch", archived_count, chat_id)

        workspace_dir = Path(agent_info.work_dir) if agent_info.work_dir else None
        message_file = self._write_handover_files(workspace_dir, operation_id, events, old_harness, target_harness)

        manager.set_handoff_state(
            chat_id,
            HandoffState(
                phase=HandoffPhase.PREPARING,
                target_harness=target_harness,
                detail=f"Starting the {target_harness.value} agent",
            ),
        )
        try:
            replacement = manager.create_replacement_agent(
                display_name=display_name,
                account_id=account_id,
                message_file=message_file,
                project_id=project_id,
            )
        except AgentCreationError as e:
            # Pre-commit rollback: the chat still points at an agent that exists and
            # has lost nothing, so lifting the freeze restores it completely.
            manager.set_chat_frozen(old_agent_id, "")
            self._fail(chat_id, target_harness, str(e))

        # THE COMMIT POINT. One lock, one atomic write, and from here the chat is the
        # new agent's; see ``ChatRegistry.begin_segment``.
        manager.chat_registry.begin_segment(chat_id, replacement.agent_id, target_harness, account_id)
        manager.set_agent_hidden(replacement.agent_id, False)
        manager.set_handoff_state(
            chat_id,
            HandoffState(phase=HandoffPhase.FINISHING, target_harness=target_harness, detail="Retiring the old agent"),
        )

        # Evict before destroying so the outgoing watcher releases its file handles and
        # watches on files that are about to be deleted underneath it.
        self.evict_watcher(old_agent_id)
        destroy_failure = manager.destroy_agent_process(AgentId(old_agent_id), old_agent_name)
        if destroy_failure is not None:
            # Not a failure of the switch: the chat has already moved. The leftover is a
            # frozen agent nobody can speak to, which the user can remove by hand.
            _loguru_logger.warning("Chat {} switched harness but its old agent lingers: {}", chat_id, destroy_failure)

        manager.set_handoff_state(chat_id, None)
        return "switched"

    def _write_handover_files(
        self,
        workspace_dir: Path | None,
        operation_id: str,
        events: list[dict[str, Any]],
        old_harness: HarnessType,
        target_harness: HarnessType,
    ) -> Path | None:
        """Write the context file and the pointer message, returning the message file.

        None means the successor starts cold. Deliberately not fatal: a chat on the
        harness the user asked for, with no handover, beats a chat stuck on the one
        they asked to leave (see ``write_handoff_context``).
        """
        if workspace_dir is None:
            return None
        context_path = write_handoff_context(workspace_dir, operation_id, list(events), old_harness, target_harness)
        if context_path is None:
            return None
        message_path = handoff_dir_for_workspace(workspace_dir, operation_id) / _FIRST_MESSAGE_FILENAME
        try:
            message_path.write_text(
                build_handoff_first_message(context_path, old_harness, target_harness), encoding="utf-8"
            )
        except OSError as e:
            _loguru_logger.opt(exception=e).warning("Failed to write the handoff first message at {}", message_path)
            return None
        return message_path

    def _fail(self, chat_id: ChatId, target_harness: HarnessType, detail: str) -> NoReturn:
        """Publish a failed switch and raise it. The chat is on its original harness."""
        self.agent_manager.set_handoff_state(
            chat_id, HandoffState(phase=HandoffPhase.FAILED, target_harness=target_harness, detail=detail)
        )
        raise HandoffError(detail, 500)
