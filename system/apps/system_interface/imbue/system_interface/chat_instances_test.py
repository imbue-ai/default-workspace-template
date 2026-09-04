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

from imbue.system_interface import member_titles
from imbue.system_interface import projects
from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.agent_manager import AgentManager
from imbue.system_interface.chat_errors import ChatCreateRefusedError
from imbue.system_interface.chat_errors import ChatTitleConflictError
from imbue.system_interface.chat_instances import AgentManagerInstanceSource
from imbue.system_interface.chat_instances import AgentManagerNudger
from imbue.system_interface.chat_instances import instance_status_for_agent
from imbue.system_interface.chat_instances import subagent_instance_key
from imbue.system_interface.models import CreatedChatAgent
from imbue.system_interface.testing import seed_agent_state
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster


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
        agent_manager._proto_agents[provisional_id] = {"agent_id": provisional_id, "name": "Chat 2"}
    source = _source(agent_manager)

    (record,) = source.list_instances()

    assert record.key == provisional_id
    assert record.title == "Chat 2"
    assert record.status is InstanceStatus.ATTENTION
    assert record.lifetime is InstanceLifetime.REFERENCED
    assert record.renameable is False


def test_a_provisional_record_becomes_the_agents_record_once_observed(agent_manager: AgentManager) -> None:
    chat_id = _agent_id()
    with agent_manager._lock:
        agent_manager._proto_agents[chat_id] = {"agent_id": chat_id, "name": "Chat 2"}
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


class _CreateRecordingAgentManager(AgentManager):
    """Answers a create with a fixed chat and keeps the taken names it was given, instead of running mngr."""

    created: CreatedChatAgent = CreatedChatAgent(agent_id=f"agent-{uuid4().hex}", name="Chat-3", display_name="Chat 3")
    last_extra_taken_names: tuple[str, ...] | None = None

    def create_chat_agent(
        self,
        requested_name: str,
        extra_role_templates: tuple[str, ...] = (),
        project_id: str = "",
        extra_taken_names: tuple[str, ...] = (),
        account_id: str = "",
    ) -> CreatedChatAgent:
        self.last_extra_taken_names = extra_taken_names
        return self.created


def test_new_counts_the_chosen_member_titles_as_taken_names(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster
) -> None:
    """A member someone renamed to "Chat 2" holds that name as surely as a chat, as the create route counts it."""
    # The fixture's environment names the layout directory the titles are read from.
    layout_dir = projects.primary_agent_layout_dir_from_env()
    assert layout_dir is not None
    member_titles.set_title(layout_dir, "terminal:terminal-1", "Chat 2")
    recording = _CreateRecordingAgentManager.build(broadcaster)
    # ``build`` is typed as returning the base class.
    assert isinstance(recording, _CreateRecordingAgentManager)
    source = _source(recording)

    record = source.create_instance(ActionId("new"), {})

    assert recording.last_extra_taken_names == ("Chat 2",)
    assert record.key == recording.created.agent_id
    assert record.title == "Chat 3"
    assert record.lifetime is InstanceLifetime.REFERENCED


def test_new_without_a_signed_in_account_is_refused(agent_manager: AgentManager) -> None:
    # The accounts root is isolated per test and holds nothing, so the create cannot bind.
    source = _source(agent_manager)
    with pytest.raises(ChatCreateRefusedError):
        source.create_instance(ActionId("new"), {})


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
        agent_manager._proto_agents[provisional_id] = {"agent_id": provisional_id, "name": "Chat 2"}
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
