"""The chat app's instances (contracts.md section 4.3, the chat row) over the agent manager.

Every chat is an ``explicit``, renameable instance keyed by its chat id, with its status
taken from the chat's snapshot (the activity state, the pending permission requests, and the
lifecycle of its active agent). A chat whose first agent mngr does not know yet (a provisional
chat) is a ``referenced`` instance under its chat id, whose status is its phase: waiting for
an account (``attention``), being created (``working``), or failed (``error``). A subagent
view is a ``referenced`` instance keyed ``<chat-id>.<agent-id>.<session-id>`` that the
parent's page creates on demand; the middle part names the agent whose harness session the
subagent belongs to.
"""

import threading
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from typing import Final

from app_instances.data_types import InstanceLifetime
from app_instances.data_types import InstanceRecord
from app_instances.data_types import InstanceStatus
from app_instances.errors import InvalidParamsError
from app_instances.errors import LocationNotTrackedError
from app_instances.errors import NotReadyError
from app_instances.errors import NotRenameableError
from app_instances.errors import NotStoppableError
from app_instances.errors import UnknownActionError
from app_instances.errors import UnknownInstanceError
from app_instances.interfaces import InstanceNudgerInterface
from app_instances.interfaces import InstanceSourceInterface
from app_instances.primitives import InstanceKey
from app_instances.primitives import InstanceTitle
from app_instances.primitives import InstanceUrl
from app_instances.primitives import LocationTarget
from app_instances.primitives import MAX_INSTANCE_TITLE_LENGTH
from app_manifest.primitives import ActionId
from app_manifest.primitives import AppName
from pydantic import Field
from pydantic import PrivateAttr

from imbue.chat.accounts import AccountError
from imbue.chat.activity_state import is_lifecycle_dead
from imbue.chat.agent_manager import AgentManager
from imbue.chat.errors import ChatCreateRefusedError
from imbue.chat.errors import ChatDestroyFailedError
from imbue.chat.errors import ChatRenameFailedError
from imbue.chat.errors import ChatStartFailedError
from imbue.chat.errors import ChatStopFailedError
from imbue.chat.errors import ChatTitleConflictError
from imbue.chat.harnesses.binding import has_usable_account
from imbue.chat.models import AgentCreationError
from imbue.chat.models import AgentDestroyError
from imbue.chat.models import AgentNameConflictError
from imbue.chat.models import AgentRenameError
from imbue.chat.models import AgentStopError
from imbue.chat.models import ChatSnapshot
from imbue.chat.models import ProvisionalChat
from imbue.chat.models import ProvisionalChatPhase
from imbue.chat.primitives import AGENT_ID_PATTERN
from imbue.chat.primitives import ChatId
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.pure import pure
from imbue.mngr.errors import MngrError

# The registered name of the chat app, which names the shell nudge route and the manifest.
CHAT_APP_NAME: Final[AppName] = AppName("chat")

NEW_ACTION_ID: Final[ActionId] = ActionId("new")
SUBAGENT_ACTION_ID: Final[ActionId] = ActionId("subagent")

# The params each action accepts (contracts.md section 2, the chat manifest row).
ACCOUNT_ID_PARAM: Final[str] = "account_id"
MESSAGE_PARAM: Final[str] = "message"
PARENT_PARAM: Final[str] = "parent"
SESSION_PARAM: Final[str] = "session"
DESCRIPTION_PARAM: Final[str] = "description"
_NEW_PARAMS: Final[frozenset[str]] = frozenset({ACCOUNT_ID_PARAM, MESSAGE_PARAM})
_SUBAGENT_PARAMS: Final[frozenset[str]] = frozenset({PARENT_PARAM, SESSION_PARAM, DESCRIPTION_PARAM})

# A subagent key is the chat id, the agent id, and the session id joined by dots, which is why
# the instance-key alphabet has a dot and the id alphabet does not.
SUBAGENT_KEY_SEPARATOR: Final[str] = "."

PROVISIONAL_TITLE: Final[str] = "New chat"
SUBAGENT_TITLE_PREFIX: Final[str] = "Subagent: "
# A subagent's description is free text from the transcript; the record keeps as much of it
# as fits the title rule beside the prefix.
MAX_SUBAGENT_DESCRIPTION_LENGTH: Final[int] = MAX_INSTANCE_TITLE_LENGTH - len(SUBAGENT_TITLE_PREFIX)


class SubagentKey(FrozenModel):
    """The three parts of a subagent view's instance key."""

    chat_id: ChatId = Field(description="The chat the subagent view belongs to")
    agent_id: str = Field(description="The agent whose harness session the subagent is a session of")
    session_id: str = Field(description="The subagent's own session id")


@pure
def instance_url_for_key(key: str) -> InstanceUrl:
    return InstanceUrl(f"/{key}")


@pure
def instance_record_for_chat(snapshot: ChatSnapshot) -> InstanceRecord:
    return InstanceRecord(
        key=InstanceKey(snapshot.chat_id),
        url=instance_url_for_key(snapshot.chat_id),
        title=InstanceTitle(snapshot.title),
        status=snapshot.status,
        lifetime=InstanceLifetime.EXPLICIT,
        last_active=None,
        renameable=True,
        stoppable=True,
    )


_STATUS_BY_PROVISIONAL_PHASE: Final[dict[ProvisionalChatPhase, InstanceStatus]] = {
    ProvisionalChatPhase.AWAITING_ACCOUNT: InstanceStatus.ATTENTION,
    ProvisionalChatPhase.CREATING: InstanceStatus.WORKING,
    ProvisionalChatPhase.FAILED: InstanceStatus.ERROR,
}


@pure
def instance_record_for_provisional_chat(provisional: ProvisionalChat) -> InstanceRecord:
    """A chat that is not an agent yet: waiting for an account, being created, or failed."""
    return InstanceRecord(
        key=InstanceKey(provisional.chat_id),
        url=instance_url_for_key(provisional.chat_id),
        title=InstanceTitle(provisional.name or PROVISIONAL_TITLE),
        status=_STATUS_BY_PROVISIONAL_PHASE[provisional.phase],
        lifetime=InstanceLifetime.REFERENCED,
        last_active=None,
        renameable=False,
    )


@pure
def subagent_instance_key(chat_id: ChatId, agent_id: str, session_id: str) -> InstanceKey:
    return InstanceKey(SUBAGENT_KEY_SEPARATOR.join((chat_id, agent_id, session_id)))


@pure
def parse_subagent_key(key: str) -> SubagentKey | None:
    """The three parts of a subagent key, or None for a key of any other shape (a chat's own key included)."""
    parts = key.split(SUBAGENT_KEY_SEPARATOR)
    if len(parts) != 3:
        return None
    chat_id, agent_id, session_id = parts
    if not AGENT_ID_PATTERN.fullmatch(chat_id) or not AGENT_ID_PATTERN.fullmatch(agent_id) or not session_id:
        return None
    return SubagentKey(chat_id=ChatId(chat_id), agent_id=agent_id, session_id=session_id)


@pure
def _is_subagent_of_a_listed_chat(subagent_key: InstanceKey, listed_chat_ids: AbstractSet[ChatId]) -> bool:
    parsed = parse_subagent_key(subagent_key)
    return parsed is not None and parsed.chat_id in listed_chat_ids


@pure
def instance_record_for_subagent(key: InstanceKey, description: str) -> InstanceRecord:
    return InstanceRecord(
        key=key,
        url=instance_url_for_key(key),
        title=InstanceTitle(f"{SUBAGENT_TITLE_PREFIX}{description}"),
        status=InstanceStatus.IDLE,
        lifetime=InstanceLifetime.REFERENCED,
        last_active=None,
        renameable=False,
    )


@pure
def _subagent_description(requested: str, session_id: str) -> str:
    """The description a subagent record keeps: the requested one, cut to fit its title, else the session id."""
    return (requested.strip() or session_id)[:MAX_SUBAGENT_DESCRIPTION_LENGTH]


@pure
def _require_params(action: ActionId, params: Mapping[str, str], allowed: frozenset[str]) -> None:
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise InvalidParamsError(f"action {action!r} does not take params {unknown}")


class AgentManagerNudger(InstanceNudgerInterface):
    """The blueprint's nudger: fires whatever nudger the manager holds, so tests stay silent."""

    model_config = {"arbitrary_types_allowed": True}

    manager: AgentManager = Field(frozen=True, description="The manager whose installed nudger is fired")

    def nudge(self) -> None:
        self.manager.nudge_shell()


class AgentManagerInstanceSource(InstanceSourceInterface):
    """The chat app's instance source over the agent manager (contracts.md section 4.3)."""

    model_config = {"arbitrary_types_allowed": True}

    manager: AgentManager = Field(frozen=True, description="The agent manager the chats are read from")
    # Starting rides the in-process mngr path a send uses to revive a stopped agent
    # (``agent_discovery.start_agent``), so a start and a message succeed or fail together.
    agent_starter: Callable[[str], None] = Field(
        frozen=True, description="Ensures the named agent is running; raises MngrError when it cannot"
    )
    # The subagent views the parent pages asked for, by key. In memory: a restart forgets
    # them, and the parent's page recreates one on demand. A record whose chat is gone is
    # dropped the next time the list is read.
    _description_by_subagent_key: dict[InstanceKey, str] = PrivateAttr(default_factory=dict)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def list_instances(self) -> list[InstanceRecord]:
        self._require_ready()
        snapshots = self.manager.get_chat_snapshots()
        records = [instance_record_for_chat(snapshot) for snapshot in snapshots]
        known_ids = {snapshot.chat_id for snapshot in snapshots}
        for provisional in self.manager.get_provisional_chats():
            if provisional.chat_id not in known_ids:
                records.append(instance_record_for_provisional_chat(provisional))
        with self._lock:
            for key in [
                key for key in self._description_by_subagent_key if not _is_subagent_of_a_listed_chat(key, known_ids)
            ]:
                del self._description_by_subagent_key[key]
            subagents = list(self._description_by_subagent_key.items())
        records.extend(instance_record_for_subagent(key, description) for key, description in subagents)
        return records

    def create_instance(self, action: ActionId, params: Mapping[str, str]) -> InstanceRecord:
        self._require_ready()
        if action == NEW_ACTION_ID:
            return self._create_chat(params)
        if action == SUBAGENT_ACTION_ID:
            return self._create_subagent(params)
        raise UnknownActionError(f"unknown action {action!r}")

    def delete_instance(self, key: InstanceKey) -> None:
        self._require_ready()
        with self._lock:
            if self._description_by_subagent_key.pop(key, None) is not None:
                return
        snapshot = self.manager.get_chat_snapshot(key)
        if snapshot is None:
            # A provisional chat that is not being created is dropped; an unknown key is a
            # no-op by contract, and so is a create in flight (the chat appears as an
            # explicit instance once its agent lands).
            self.manager.discard_provisional_chat(key)
            return
        try:
            self.manager.destroy_chat(snapshot.chat_id)
        except AgentDestroyError as e:
            raise ChatDestroyFailedError(str(e)) from e

    def rename_instance(self, key: InstanceKey, title: InstanceTitle) -> InstanceRecord:
        self._require_ready()
        snapshot = self.manager.get_chat_snapshot(key)
        if snapshot is None:
            if self._is_provisional_or_subagent(key):
                raise NotRenameableError(f"instance {key!r} cannot be renamed until its chat exists")
            raise UnknownInstanceError(f"no instance has the key {key!r}")
        try:
            self.manager.rename_chat(snapshot.chat_id, title)
        except AgentNameConflictError as e:
            raise ChatTitleConflictError(str(e)) from e
        except AgentRenameError as e:
            raise ChatRenameFailedError(str(e)) from e
        return self._record_for_chat(snapshot.chat_id, None)

    def set_location(self, key: InstanceKey, path: LocationTarget) -> InstanceRecord:
        raise LocationNotTrackedError("the chat app does not track where its pages are")

    def stop_instance(self, key: InstanceKey) -> InstanceRecord:
        """``mngr stop`` for a chat: the process ends, the transcript and name stay, and the record answers ``stopped``."""
        snapshot = self._stoppable_chat(key)
        if not is_lifecycle_dead(snapshot.active_agent.state):
            try:
                self.manager.stop_chat(snapshot.chat_id)
            except AgentStopError as e:
                raise ChatStopFailedError(str(e)) from e
        return self._record_for_chat(snapshot.chat_id, InstanceStatus.STOPPED)

    def start_instance(self, key: InstanceKey) -> InstanceRecord:
        """Ensure a chat's active agent is running, the same in-process path a send takes to revive one; a no-op for a live chat."""
        snapshot = self._stoppable_chat(key)
        if is_lifecycle_dead(snapshot.active_agent.state):
            try:
                self.agent_starter(snapshot.active_agent.name)
            except MngrError as e:
                raise ChatStartFailedError(f"Failed to start agent '{snapshot.active_agent.name}': {e}") from e
            # The observe stream sees the revival only minutes later (no pid to watch while the
            # agent was stopped); reflect it now so the record and the page's liveness follow the start.
            self.manager.note_agent_alive(snapshot.active_agent.agent_id)
        return self._record_for_chat(snapshot.chat_id, None)

    def _stoppable_chat(self, key: InstanceKey) -> ChatSnapshot:
        """The chat the verb acts on: a listed chat, never a provisional chat or a subagent view."""
        self._require_ready()
        snapshot = self.manager.get_chat_snapshot(key)
        if snapshot is None:
            if self._is_provisional_or_subagent(key):
                raise NotStoppableError(f"instance {key!r} has no agent process to stop or start")
            raise UnknownInstanceError(f"no instance has the key {key!r}")
        return snapshot

    def _record_for_chat(self, chat_id: ChatId, status_override: InstanceStatus | None) -> InstanceRecord:
        """The chat's record as tracked now; ``status_override`` reports a state the observe stream will only confirm later."""
        snapshot = self.manager.get_chat_snapshot(chat_id)
        if snapshot is None:
            raise UnknownInstanceError(f"no instance has the key {chat_id!r}")
        record = instance_record_for_chat(snapshot)
        if status_override is None:
            return record
        return record.model_copy_update(to_update(record.field_ref().status, status_override))

    def _create_chat(self, params: Mapping[str, str]) -> InstanceRecord:
        """``new``: launch a chat on ``account_id`` (the most recently used account when the
        param is absent), or, with nothing signed in, mint one that waits for an account: its
        page shows the provider chooser, so the tab opens either way. ``message`` is the first
        message the chat sends once it runs; a waiting chat keeps it for its launch."""
        _require_params(NEW_ACTION_ID, params, _NEW_PARAMS)
        account_id = params.get(ACCOUNT_ID_PARAM, "")
        message = params.get(MESSAGE_PARAM, "")
        try:
            is_reserved = not account_id and not has_usable_account()
        except AccountError as e:
            # The account store is unreadable: the same refusal the manager gives when it
            # cannot resolve a binding, so the shell shows the reason rather than a 500.
            raise ChatCreateRefusedError(str(e)) from e
        if is_reserved:
            created = self.manager.reserve_chat(message=message)
        else:
            try:
                created = self.manager.create_chat(
                    requested_name="",
                    extra_role_templates=(),
                    project_id="",
                    account_id=account_id,
                    message=message,
                )
            except AgentCreationError as e:
                raise ChatCreateRefusedError(str(e)) from e
        provisional = self.manager.get_provisional_chat(created.chat_id)
        if provisional is not None:
            return instance_record_for_provisional_chat(provisional)
        # The create can land before this reads the record back (the creation thread drops it
        # the moment ``mngr create`` exits 0): the chat is then an ordinary instance.
        landed = self.manager.get_chat_snapshot(created.chat_id)
        if landed is None:
            raise ChatCreateRefusedError(f"chat {created.chat_id} vanished before it could be listed")
        return instance_record_for_chat(landed)

    def _create_subagent(self, params: Mapping[str, str]) -> InstanceRecord:
        _require_params(SUBAGENT_ACTION_ID, params, _SUBAGENT_PARAMS)
        parent = params.get(PARENT_PARAM, "")
        session = params.get(SESSION_PARAM, "")
        if not parent or not session:
            raise InvalidParamsError(f"action {SUBAGENT_ACTION_ID!r} requires {PARENT_PARAM!r} and {SESSION_PARAM!r}")
        parent_chat = self.manager.get_chat_snapshot(parent) if AGENT_ID_PATTERN.fullmatch(parent) else None
        if parent_chat is None:
            raise InvalidParamsError(f"{PARENT_PARAM!r} {parent!r} is not a chat this app lists")
        # The session belongs to the chat's active agent: a subagent is opened from the live
        # transcript, and the key names that agent so the view keeps reading the right files.
        key = subagent_instance_key(parent_chat.chat_id, parent_chat.active_agent.agent_id, session)
        with self._lock:
            description = self._description_by_subagent_key.get(key)
            if description is None:
                description = _subagent_description(params.get(DESCRIPTION_PARAM, ""), session)
                self._description_by_subagent_key[key] = description
        return instance_record_for_subagent(key, description)

    def _is_provisional_or_subagent(self, key: InstanceKey) -> bool:
        with self._lock:
            if key in self._description_by_subagent_key:
                return True
        return self.manager.get_provisional_chat(key) is not None

    def _require_ready(self) -> None:
        if not self.manager.is_agent_list_known():
            raise NotReadyError("the chat app has not read its agent list from mngr yet")


def build_chat_instance_source(
    manager: AgentManager, agent_starter: Callable[[str], None]
) -> tuple[InstanceSourceInterface, InstanceNudgerInterface]:
    """The source and nudger the chat document mounts the instances blueprint with."""
    return AgentManagerInstanceSource(manager=manager, agent_starter=agent_starter), AgentManagerNudger(
        manager=manager
    )
