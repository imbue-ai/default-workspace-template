"""The chat app's instances (contracts.md section 4.3, the chat row) over the agent manager.

Every non-primary agent is an ``explicit``, renameable instance keyed by its agent id, with
its status derived from the activity state, the pending permission requests, and the
lifecycle. A chat still being created (a proto agent) is a ``referenced`` provisional
instance under the id mngr will give it, and a subagent view is a ``referenced`` instance
keyed ``<agent-id>.<session-id>`` that the parent's page creates on demand.
"""

import re
import threading
from collections.abc import Mapping
from typing import Final

from app_instances.data_types import InstanceLifetime
from app_instances.data_types import InstanceRecord
from app_instances.data_types import InstanceStatus
from app_instances.errors import InvalidParamsError
from app_instances.errors import LocationNotTrackedError
from app_instances.errors import NotReadyError
from app_instances.errors import NotRenameableError
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

from imbue.imbue_common.pure import pure
from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.activity_state import is_lifecycle_dead
from imbue.system_interface.agent_manager import AgentManager
from imbue.system_interface.chat_errors import ChatCreateRefusedError
from imbue.system_interface.chat_errors import ChatDestroyFailedError
from imbue.system_interface.chat_errors import ChatRenameFailedError
from imbue.system_interface.chat_errors import ChatTitleConflictError
from imbue.system_interface.models import AgentCreationError
from imbue.system_interface.models import AgentDestroyError
from imbue.system_interface.models import AgentNameConflictError
from imbue.system_interface.models import AgentRenameError
from imbue.system_interface.models import AgentStateItem

# The registered name of the chat app, which names the shell nudge route and the manifest.
CHAT_APP_NAME: Final[AppName] = AppName("chat")

NEW_ACTION_ID: Final[ActionId] = ActionId("new")
SUBAGENT_ACTION_ID: Final[ActionId] = ActionId("subagent")

# The params each action accepts (contracts.md section 2, the chat manifest row).
ACCOUNT_ID_PARAM: Final[str] = "account_id"
PARENT_PARAM: Final[str] = "parent"
SESSION_PARAM: Final[str] = "session"
DESCRIPTION_PARAM: Final[str] = "description"
_NEW_PARAMS: Final[frozenset[str]] = frozenset({ACCOUNT_ID_PARAM})
_SUBAGENT_PARAMS: Final[frozenset[str]] = frozenset({PARENT_PARAM, SESSION_PARAM, DESCRIPTION_PARAM})

# An agent id: ``agent-<32 hex>`` as mngr mints it, with the instance-key alphabet so a test
# fixture's id counts too. A subagent key is one followed by a dot and the session id, which
# is why the key alphabet has a dot.
AGENT_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^agent-[A-Za-z0-9_-]{1,120}$")
SUBAGENT_KEY_SEPARATOR: Final[str] = "."

PROVISIONAL_TITLE: Final[str] = "New chat"
SUBAGENT_TITLE_PREFIX: Final[str] = "Subagent: "
# A subagent's description is free text from the transcript; the record keeps as much of it
# as fits the title rule beside the prefix.
MAX_SUBAGENT_DESCRIPTION_LENGTH: Final[int] = MAX_INSTANCE_TITLE_LENGTH - len(SUBAGENT_TITLE_PREFIX)


@pure
def instance_status_for_agent(
    lifecycle_state: str, activity_state: ActivityState | None, is_permission_pending: bool
) -> InstanceStatus:
    """The chat row's status rule: a dead lifecycle wins, then a pending permission, then a live turn."""
    if is_lifecycle_dead(lifecycle_state):
        return InstanceStatus.STOPPED
    if is_permission_pending:
        return InstanceStatus.ATTENTION
    if activity_state in (ActivityState.THINKING, ActivityState.TOOL_RUNNING):
        return InstanceStatus.WORKING
    return InstanceStatus.IDLE


@pure
def instance_url_for_key(key: str) -> InstanceUrl:
    return InstanceUrl(f"/{key}")


@pure
def display_title_for_agent(agent: AgentStateItem) -> InstanceTitle:
    """The name the user gave the chat (its ``display_name`` label), else its true name."""
    return InstanceTitle(agent.labels.get("display_name") or agent.name)


@pure
def instance_record_for_agent(agent: AgentStateItem, is_permission_pending: bool) -> InstanceRecord:
    return InstanceRecord(
        key=InstanceKey(agent.id),
        url=instance_url_for_key(agent.id),
        title=display_title_for_agent(agent),
        status=instance_status_for_agent(agent.state, agent.activity_state, is_permission_pending),
        lifetime=InstanceLifetime.EXPLICIT,
        last_active=None,
        renameable=True,
    )


@pure
def instance_record_for_provisional_chat(agent_id: str, name: str) -> InstanceRecord:
    """A chat whose ``mngr create`` is still running: the page shows its creation log."""
    return InstanceRecord(
        key=InstanceKey(agent_id),
        url=instance_url_for_key(agent_id),
        title=InstanceTitle(name or PROVISIONAL_TITLE),
        status=InstanceStatus.ATTENTION,
        lifetime=InstanceLifetime.REFERENCED,
        last_active=None,
        renameable=False,
    )


@pure
def subagent_instance_key(parent_agent_id: str, session_id: str) -> InstanceKey:
    return InstanceKey(f"{parent_agent_id}{SUBAGENT_KEY_SEPARATOR}{session_id}")


@pure
def _parent_agent_id(subagent_key: InstanceKey) -> str:
    return subagent_key.partition(SUBAGENT_KEY_SEPARATOR)[0]


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
def is_primary_agent(agent: AgentStateItem) -> bool:
    return agent.labels.get("is_primary") == "true"


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

    manager: AgentManager = Field(frozen=True, description="The agent manager the instances are read from")
    # The subagent views the parent pages asked for, by key. In memory, as the phase file
    # says: a restart forgets them, and the parent's page recreates one on demand. A record
    # whose parent chat is gone is dropped the next time the list is read.
    _description_by_subagent_key: dict[InstanceKey, str] = PrivateAttr(default_factory=dict)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def list_instances(self) -> list[InstanceRecord]:
        self._require_ready()
        agents = [agent for agent in self.manager.get_agents() if not is_primary_agent(agent)]
        records = [instance_record_for_agent(agent, self.manager.has_pending_permission(agent.id)) for agent in agents]
        known_ids = {agent.id for agent in agents}
        for proto in self.manager.get_proto_agents():
            agent_id = str(proto.get("agent_id", ""))
            if agent_id and agent_id not in known_ids:
                records.append(instance_record_for_provisional_chat(agent_id, str(proto.get("name", ""))))
        with self._lock:
            for key in [key for key in self._description_by_subagent_key if _parent_agent_id(key) not in known_ids]:
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
        agent = self.manager.get_agent_by_id(key)
        if agent is None or is_primary_agent(agent):
            # An unknown key is a no-op by contract, and so is a provisional chat: its
            # ``mngr create`` is already running and the agent appears as an explicit
            # instance once it lands.
            return
        try:
            self.manager.destroy_chat_agent(key)
        except AgentDestroyError as e:
            raise ChatDestroyFailedError(str(e)) from e

    def rename_instance(self, key: InstanceKey, title: InstanceTitle) -> InstanceRecord:
        self._require_ready()
        agent = self.manager.get_agent_by_id(key)
        if agent is None or is_primary_agent(agent):
            if self._is_provisional_or_subagent(key):
                raise NotRenameableError(f"instance {key!r} cannot be renamed until its chat exists")
            raise UnknownInstanceError(f"no instance has the key {key!r}")
        try:
            self.manager.rename_chat_agent(key, title)
        except AgentNameConflictError as e:
            raise ChatTitleConflictError(str(e)) from e
        except AgentRenameError as e:
            raise ChatRenameFailedError(str(e)) from e
        renamed = self.manager.get_agent_by_id(key)
        if renamed is None:
            raise UnknownInstanceError(f"no instance has the key {key!r}")
        return instance_record_for_agent(renamed, self.manager.has_pending_permission(key))

    def set_location(self, key: InstanceKey, path: LocationTarget) -> InstanceRecord:
        raise LocationNotTrackedError("the chat app does not track where its pages are")

    def _create_chat(self, params: Mapping[str, str]) -> InstanceRecord:
        _require_params(NEW_ACTION_ID, params, _NEW_PARAMS)
        try:
            created = self.manager.create_chat_agent(
                requested_name="",
                extra_role_templates=(),
                project_id="",
                extra_taken_names=(),
                account_id=params.get(ACCOUNT_ID_PARAM, ""),
            )
        except AgentCreationError as e:
            raise ChatCreateRefusedError(str(e)) from e
        return instance_record_for_provisional_chat(created.agent_id, created.display_name)

    def _create_subagent(self, params: Mapping[str, str]) -> InstanceRecord:
        _require_params(SUBAGENT_ACTION_ID, params, _SUBAGENT_PARAMS)
        parent = params.get(PARENT_PARAM, "")
        session = params.get(SESSION_PARAM, "")
        if not parent or not session:
            raise InvalidParamsError(f"action {SUBAGENT_ACTION_ID!r} requires {PARENT_PARAM!r} and {SESSION_PARAM!r}")
        parent_agent = self.manager.get_agent_by_id(parent) if AGENT_ID_PATTERN.fullmatch(parent) else None
        if parent_agent is None or is_primary_agent(parent_agent):
            raise InvalidParamsError(f"{PARENT_PARAM!r} {parent!r} is not a chat this app lists")
        key = subagent_instance_key(parent, session)
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
        return any(str(proto.get("agent_id", "")) == key for proto in self.manager.get_proto_agents())

    def _require_ready(self) -> None:
        if not self.manager.is_agent_list_known():
            raise NotReadyError("the chat app has not read its agent list from mngr yet")


def build_chat_instance_source(manager: AgentManager) -> tuple[InstanceSourceInterface, InstanceNudgerInterface]:
    """The source and nudger the chat document mounts the instances blueprint with."""
    return AgentManagerInstanceSource(manager=manager), AgentManagerNudger(manager=manager)
