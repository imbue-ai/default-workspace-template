from uuid import uuid4

import pytest
from app_instances.data_types import InstanceLifetime
from app_instances.data_types import InstanceStatus
from app_instances.errors import InvalidParamsError
from app_instances.errors import LocationNotTrackedError
from app_instances.errors import NotReadyError
from app_instances.errors import NotRenameableError
from app_instances.errors import UnknownActionError
from app_instances.errors import UnknownInstanceError
from app_instances.primitives import InstanceKey
from app_instances.primitives import InstanceTitle
from app_instances.primitives import LocationPath
from app_instances.primitives import MAX_INSTANCE_TITLE_LENGTH
from app_instances.testing import RecordingNudger
from app_manifest.primitives import ActionId

from imbue.chat.accounts import index_path
from imbue.chat.activity_state import ActivityState
from imbue.chat.agent_manager import AgentManager
from imbue.chat.errors import ChatCreateRefusedError
from imbue.chat.errors import ChatTitleConflictError
from imbue.chat.instances import AgentManagerInstanceSource
from imbue.chat.instances import AgentManagerNudger
from imbue.chat.instances import instance_status_for_agent
from imbue.chat.instances import subagent_instance_key
from imbue.chat.models import CreatedChatAgent
from imbue.chat.models import ProvisionalChat
from imbue.chat.models import ProvisionalChatPhase
from imbue.chat.testing import seed_agent_state
from imbue.chat.ws_broadcaster import WebSocketBroadcaster


def _agent_id() -> str:
    return f"agent-{uuid4().hex}"


def _seed_agent(
    manager: AgentManager,
    agent_id: str,
    name: str,
    *,
    labels: dict[str, str] | None = None,
    activity_state: ActivityState | None = None,
) -> None:
    """A tracked chat named ``name``, whose display name is the spaced form unless ``labels`` says otherwise."""
    seed_agent_state(
        manager,
        agent_id,
        name=name,
        labels=labels if labels is not None else {"display_name": name.replace("-", " ")},
        activity_state=activity_state,
    )


def _creating(
    name: str, agent_id: str, phase: ProvisionalChatPhase = ProvisionalChatPhase.CREATING
) -> ProvisionalChat:
    return ProvisionalChat(agent_id=agent_id, name=name, phase=phase)


def _source(agent_manager: AgentManager) -> AgentManagerInstanceSource:
    agent_manager.note_agent_list_known()
    return AgentManagerInstanceSource(manager=agent_manager)


@pytest.mark.parametrize(
    ("lifecycle", "activity", "is_permission_pending", "expected"),
    [
        ("RUNNING", ActivityState.THINKING, False, InstanceStatus.WORKING),
        ("RUNNING", ActivityState.TOOL_RUNNING, False, InstanceStatus.WORKING),
        ("RUNNING", ActivityState.IDLE, False, InstanceStatus.IDLE),
        ("WAITING", None, False, InstanceStatus.IDLE),
        ("UNKNOWN", ActivityState.THINKING, False, InstanceStatus.WORKING),
        ("RUNNING", ActivityState.THINKING, True, InstanceStatus.ATTENTION),
        ("STOPPED", ActivityState.THINKING, True, InstanceStatus.STOPPED),
        ("DONE", None, False, InstanceStatus.STOPPED),
    ],
)
def test_status_mapping_follows_the_chat_row(
    lifecycle: str, activity: ActivityState | None, is_permission_pending: bool, expected: InstanceStatus
) -> None:
    assert instance_status_for_agent(lifecycle, activity, is_permission_pending) is expected


def test_list_is_not_ready_before_the_agent_list_is_known(agent_manager: AgentManager) -> None:
    source = AgentManagerInstanceSource(manager=agent_manager)
    with pytest.raises(NotReadyError):
        source.list_instances()
    with pytest.raises(NotReadyError):
        source.delete_instance(InstanceKey(_agent_id()))


def test_list_maps_every_non_primary_agent(agent_manager: AgentManager) -> None:
    chat_id = _agent_id()
    primary_id = _agent_id()
    _seed_agent(agent_manager, chat_id, "Chat-1", activity_state=ActivityState.THINKING)
    _seed_agent(agent_manager, primary_id, "services", labels={"is_primary": "true"})
    source = _source(agent_manager)

    records = source.list_instances()

    assert [record.key for record in records] == [chat_id]
    record = records[0]
    assert record.url == f"/{chat_id}"
    assert record.title == "Chat 1"
    assert record.status is InstanceStatus.WORKING
    assert record.lifetime is InstanceLifetime.EXPLICIT
    assert record.renameable is True
    assert record.last_active is None


def test_title_falls_back_to_the_true_name_without_a_display_label(agent_manager: AgentManager) -> None:
    chat_id = _agent_id()
    _seed_agent(agent_manager, chat_id, "Chat-1", labels={})
    assert _source(agent_manager).list_instances()[0].title == "Chat-1"


def test_a_pending_permission_shows_as_attention(agent_manager: AgentManager) -> None:
    chat_id = _agent_id()
    _seed_agent(agent_manager, chat_id, "Chat-1", activity_state=ActivityState.THINKING)
    with agent_manager._lock:
        agent_manager._pending_permission_ids_by_agent[chat_id] = {"evt-1"}
    assert _source(agent_manager).list_instances()[0].status is InstanceStatus.ATTENTION


def test_a_chat_being_created_is_a_provisional_instance(agent_manager: AgentManager) -> None:
    provisional_id = _agent_id()
    with agent_manager._lock:
        agent_manager._proto_agents[provisional_id] = _creating("Chat 2", provisional_id)
    source = _source(agent_manager)

    (record,) = source.list_instances()

    assert record.key == provisional_id
    assert record.title == "Chat 2"
    assert record.status is InstanceStatus.WORKING
    assert record.lifetime is InstanceLifetime.REFERENCED
    assert record.renameable is False


@pytest.mark.parametrize(
    ("phase", "status"),
    [
        (ProvisionalChatPhase.AWAITING_ACCOUNT, InstanceStatus.ATTENTION),
        (ProvisionalChatPhase.FAILED, InstanceStatus.ERROR),
    ],
)
def test_a_provisional_chats_status_follows_its_phase(
    agent_manager: AgentManager, phase: ProvisionalChatPhase, status: InstanceStatus
) -> None:
    provisional_id = _agent_id()
    with agent_manager._lock:
        agent_manager._proto_agents[provisional_id] = _creating("Chat 2", provisional_id, phase)
    (record,) = _source(agent_manager).list_instances()
    assert record.status is status


def test_a_provisional_record_becomes_the_agents_record_once_observed(agent_manager: AgentManager) -> None:
    chat_id = _agent_id()
    with agent_manager._lock:
        agent_manager._proto_agents[chat_id] = _creating("Chat 2", chat_id)
    source = _source(agent_manager)
    _seed_agent(agent_manager, chat_id, "Chat-2")

    (record,) = source.list_instances()

    assert record.lifetime is InstanceLifetime.EXPLICIT
    assert record.renameable is True


def test_subagent_create_is_idempotent_and_listed(agent_manager: AgentManager) -> None:
    parent_id = _agent_id()
    _seed_agent(agent_manager, parent_id, "Chat-1")
    source = _source(agent_manager)
    session = uuid4().hex

    first = source.create_instance(
        ActionId("subagent"), {"parent": parent_id, "session": session, "description": "Explore the repo"}
    )
    second = source.create_instance(ActionId("subagent"), {"parent": parent_id, "session": session})

    assert first == second
    assert first.key == subagent_instance_key(parent_id, session)
    assert first.url == f"/{parent_id}.{session}"
    assert first.title == "Subagent: Explore the repo"
    assert first.status is InstanceStatus.IDLE
    assert first.lifetime is InstanceLifetime.REFERENCED
    assert first.renameable is False
    assert [record.key for record in source.list_instances()] == [parent_id, first.key]


def test_subagent_description_is_cut_to_fit_the_title_and_defaults_to_the_session(
    agent_manager: AgentManager,
) -> None:
    parent_id = _agent_id()
    _seed_agent(agent_manager, parent_id, "Chat-1")
    source = _source(agent_manager)
    session = uuid4().hex

    long = source.create_instance(
        ActionId("subagent"), {"parent": parent_id, "session": session, "description": "x" * 400}
    )
    blank = source.create_instance(
        ActionId("subagent"), {"parent": parent_id, "session": "other", "description": "  "}
    )

    assert len(long.title) == MAX_INSTANCE_TITLE_LENGTH
    assert long.title.startswith("Subagent: xxx")
    assert blank.title == "Subagent: other"


def test_subagent_create_requires_a_listed_parent_and_both_params(agent_manager: AgentManager) -> None:
    parent_id = _agent_id()
    primary_id = _agent_id()
    _seed_agent(agent_manager, parent_id, "Chat-1")
    _seed_agent(agent_manager, primary_id, "services", labels={"is_primary": "true"})
    source = _source(agent_manager)
    with pytest.raises(InvalidParamsError):
        source.create_instance(ActionId("subagent"), {"parent": parent_id})
    with pytest.raises(InvalidParamsError):
        source.create_instance(ActionId("subagent"), {"session": "abc"})
    with pytest.raises(InvalidParamsError):
        source.create_instance(ActionId("subagent"), {"parent": _agent_id(), "session": "abc"})
    # The primary services agent is tracked but never listed, so it is no parent either.
    with pytest.raises(InvalidParamsError):
        source.create_instance(ActionId("subagent"), {"parent": primary_id, "session": "abc"})
    with pytest.raises(InvalidParamsError):
        source.create_instance(ActionId("subagent"), {"parent": parent_id, "session": "abc", "extra": "x"})


def test_unknown_action_and_params_are_refused(agent_manager: AgentManager) -> None:
    source = _source(agent_manager)
    with pytest.raises(UnknownActionError):
        source.create_instance(ActionId("open"), {})
    with pytest.raises(InvalidParamsError):
        source.create_instance(ActionId("new"), {"workdir": "/tmp"})


def test_new_without_a_signed_in_account_reserves_a_chat_awaiting_one(agent_manager: AgentManager) -> None:
    """The tab opens either way: with nothing signed in the chat waits for an account, and its
    page shows the provider chooser."""
    # The accounts root is isolated per test and holds nothing, so the create cannot bind.
    source = _source(agent_manager)

    record = source.create_instance(ActionId("new"), {})

    assert record.status is InstanceStatus.ATTENTION
    assert record.lifetime is InstanceLifetime.REFERENCED
    assert record.title == "Chat 1"
    proto = agent_manager.get_proto_agent(record.key)
    assert proto is not None
    assert proto.phase is ProvisionalChatPhase.AWAITING_ACCOUNT
    assert [candidate.key for candidate in source.list_instances()] == [record.key]


def test_new_over_an_unreadable_account_index_is_refused_with_the_reason(agent_manager: AgentManager) -> None:
    """A corrupt index is the account store's failure, answered like every other refusal (a 409
    with a detail through the blueprint) rather than escaping as a 500."""
    index_path().parent.mkdir(parents=True, exist_ok=True)
    index_path().write_text("{not json")
    source = _source(agent_manager)

    with pytest.raises(ChatCreateRefusedError, match="unreadable"):
        source.create_instance(ActionId("new"), {})

    assert agent_manager.get_proto_agents() == []


def test_new_with_an_account_named_that_does_not_exist_is_refused(agent_manager: AgentManager) -> None:
    source = _source(agent_manager)
    with pytest.raises(ChatCreateRefusedError):
        source.create_instance(ActionId("new"), {"account_id": "no-such-account"})


class _LandingAgentManager(AgentManager):
    """A manager whose create has landed by the time it answers: the agent is registered under
    the id it returns and no provisional record is left, as a ``mngr create`` that exits before
    the instances API reads the record back leaves things."""

    def create_chat_agent(
        self,
        requested_name: str,
        extra_role_templates: tuple[str, ...] = (),
        project_id: str = "",
        account_id: str = "",
        agent_id: str = "",
    ) -> CreatedChatAgent:
        landed_id = _agent_id()
        _seed_agent(self, landed_id, "Chat-1")
        return CreatedChatAgent(agent_id=landed_id, name="Chat-1", display_name="Chat 1")


class _VanishingAgentManager(AgentManager):
    """A manager whose create answers an id that is neither a provisional record nor an agent."""

    def create_chat_agent(
        self,
        requested_name: str,
        extra_role_templates: tuple[str, ...] = (),
        project_id: str = "",
        account_id: str = "",
        agent_id: str = "",
    ) -> CreatedChatAgent:
        return CreatedChatAgent(agent_id=_agent_id(), name="Chat-1", display_name="Chat 1")


def test_new_answers_the_agents_own_record_when_the_create_lands_before_it_is_read_back() -> None:
    """The creation thread drops the provisional record the moment ``mngr create`` exits 0, which
    can be before the route reads it back: the chat is then an ordinary, explicit instance."""
    source = _source(_LandingAgentManager.build(WebSocketBroadcaster()))

    record = source.create_instance(ActionId("new"), {"account_id": "acct-1"})

    assert record.lifetime is InstanceLifetime.EXPLICIT
    assert record.renameable is True
    assert record.title == "Chat 1"
    assert [candidate.key for candidate in source.list_instances()] == [record.key]


def test_new_is_refused_when_the_created_chat_is_nowhere_to_be_listed() -> None:
    source = _source(_VanishingAgentManager.build(WebSocketBroadcaster()))

    with pytest.raises(ChatCreateRefusedError, match="vanished"):
        source.create_instance(ActionId("new"), {"account_id": "acct-1"})


def test_delete_drops_a_reserved_chat_and_leaves_a_create_in_flight_alone(agent_manager: AgentManager) -> None:
    source = _source(agent_manager)
    reserved = source.create_instance(ActionId("new"), {})
    creating_id = _agent_id()
    with agent_manager._lock:
        agent_manager._proto_agents[creating_id] = _creating("Chat 2", creating_id)

    source.delete_instance(reserved.key)
    source.delete_instance(InstanceKey(creating_id))

    assert [candidate.key for candidate in source.list_instances()] == [creating_id]


def test_delete_drops_a_failed_chat(agent_manager: AgentManager) -> None:
    source = _source(agent_manager)
    failed_id = _agent_id()
    with agent_manager._lock:
        agent_manager._proto_agents[failed_id] = _creating("Chat 3", failed_id, ProvisionalChatPhase.FAILED)
    assert [candidate.status for candidate in source.list_instances()] == [InstanceStatus.ERROR]

    source.delete_instance(InstanceKey(failed_id))

    assert source.list_instances() == []


def test_delete_drops_a_subagent_record_and_ignores_unknown_keys(agent_manager: AgentManager) -> None:
    parent_id = _agent_id()
    _seed_agent(agent_manager, parent_id, "Chat-1")
    source = _source(agent_manager)
    record = source.create_instance(ActionId("subagent"), {"parent": parent_id, "session": uuid4().hex})

    source.delete_instance(record.key)
    source.delete_instance(InstanceKey(_agent_id()))

    assert [candidate.key for candidate in source.list_instances()] == [parent_id]


def test_a_subagent_record_goes_with_its_destroyed_parent(agent_manager: AgentManager) -> None:
    parent_id = _agent_id()
    survivor_id = _agent_id()
    _seed_agent(agent_manager, parent_id, "Chat-1")
    _seed_agent(agent_manager, survivor_id, "Chat-2")
    source = _source(agent_manager)
    orphaned = source.create_instance(ActionId("subagent"), {"parent": parent_id, "session": uuid4().hex})
    kept = source.create_instance(ActionId("subagent"), {"parent": survivor_id, "session": uuid4().hex})

    agent_manager.remove_agent(parent_id)

    assert [record.key for record in source.list_instances()] == [survivor_id, kept.key]
    # A page asking for the orphan again gets a fresh record rather than the stale one.
    with pytest.raises(InvalidParamsError):
        source.create_instance(ActionId("subagent"), {"parent": parent_id, "session": orphaned.key.split(".")[1]})


def test_delete_never_touches_the_primary_agent(agent_manager: AgentManager) -> None:
    primary_id = _agent_id()
    _seed_agent(agent_manager, primary_id, "services", labels={"is_primary": "true"})
    source = _source(agent_manager)
    source.delete_instance(InstanceKey(primary_id))
    assert agent_manager.get_agent_by_id(primary_id) is not None


def test_rename_is_refused_for_provisional_and_subagent_keys(agent_manager: AgentManager) -> None:
    parent_id = _agent_id()
    provisional_id = _agent_id()
    _seed_agent(agent_manager, parent_id, "Chat-1")
    with agent_manager._lock:
        agent_manager._proto_agents[provisional_id] = _creating("Chat 2", provisional_id)
    source = _source(agent_manager)
    subagent = source.create_instance(ActionId("subagent"), {"parent": parent_id, "session": uuid4().hex})

    with pytest.raises(NotRenameableError):
        source.rename_instance(subagent.key, InstanceTitle("Other"))
    with pytest.raises(NotRenameableError):
        source.rename_instance(InstanceKey(provisional_id), InstanceTitle("Other"))
    with pytest.raises(UnknownInstanceError):
        source.rename_instance(InstanceKey(_agent_id()), InstanceTitle("Other"))


def test_rename_conflict_is_a_conflict(agent_manager: AgentManager) -> None:
    first_id = _agent_id()
    second_id = _agent_id()
    _seed_agent(agent_manager, first_id, "Chat-1")
    _seed_agent(agent_manager, second_id, "Chat-2")
    source = _source(agent_manager)
    with pytest.raises(ChatTitleConflictError):
        source.rename_instance(InstanceKey(second_id), InstanceTitle("Chat 1"))


def test_location_is_not_tracked(agent_manager: AgentManager) -> None:
    source = _source(agent_manager)
    with pytest.raises(LocationNotTrackedError):
        source.set_location(InstanceKey(_agent_id()), LocationPath("/somewhere"))


def test_the_manager_nudger_fires_whatever_nudger_the_manager_holds(agent_manager: AgentManager) -> None:
    recording = RecordingNudger()
    agent_manager.set_nudger(recording)
    AgentManagerNudger(manager=agent_manager).nudge()
    assert recording.nudge_count == 1
