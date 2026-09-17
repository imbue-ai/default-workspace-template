"""Tests for the AgentManager."""

import json
import os
import queue
import shutil
import signal
import time
import tomllib
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from app_instances.data_types import InstanceStatus
from app_instances.testing import RecordingNudger
from app_instances.testing import wait_until
from mngr_cli_contract.contract import assert_mngr_argv_valid
from oom_priority import bands

from imbue.chat.accounts import Account
from imbue.chat.accounts import account_dir
from imbue.chat.accounts import commit_account
from imbue.chat.accounts import delete_account
from imbue.chat.accounts import mint_account_dir
from imbue.chat.accounts import read_index
from imbue.chat.activity_state import ActivityState
from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.agent_manager import AgentManager
from imbue.chat.agent_manager import FULL_SNAPSHOTS_BEFORE_A_CREATED_AGENT_IS_LET_GO
from imbue.chat.agent_manager import HandoffCapabilities
from imbue.chat.agent_manager import _SwitchTarget
from imbue.chat.agent_manager import _build_chat_create_command
from imbue.chat.agent_manager import _build_chat_display_label_command
from imbue.chat.agent_manager import _build_chat_rename_command
from imbue.chat.agent_manager import _build_observe_command_argv
from imbue.chat.agent_manager import _chat_project_label
from imbue.chat.agent_manager import _rename_failure_detail
from imbue.chat.agent_manager import is_rebind_target
from imbue.chat.agent_manager import launch_role_templates
from imbue.chat.auto_open import AutoOpenLedger
from imbue.chat.auto_open import AutoOpenReactor
from imbue.chat.autocompact import ChatAutoCompactor
from imbue.chat.chat_fast_mode import ChatFastModeState
from imbue.chat.chat_fast_mode import read_fast_mode_state
from imbue.chat.chat_handoffs import SuccessorCreateSpec
from imbue.chat.chat_rebinds import RebindCancelledError
from imbue.chat.chat_records import ChatRecord
from imbue.chat.chat_records import ChatRecordError
from imbue.chat.chat_records import FileChatRecordStore
from imbue.chat.chat_records import InMemoryChatRecordStore
from imbue.chat.chat_seed import SeedRole
from imbue.chat.chat_seed import SeedTurn
from imbue.chat.chat_seed import read_seed_events
from imbue.chat.chat_seed import seed_event_id
from imbue.chat.chat_settings import ChatSettings
from imbue.chat.chat_settings import ChatSettingsStore
from imbue.chat.chat_settings import FastModeMode
from imbue.chat.harnesses.codex.activity import CodexActivityTracker
from imbue.chat.harnesses.codex.model import codex_models_to_options
from imbue.chat.harnesses.codex.model import get_codex_model_options_path
from imbue.chat.harnesses.codex.model import write_codex_model_options
from imbue.chat.harnesses.events import SPECIAL_EVENT_TYPE
from imbue.chat.harnesses.events import SpecialEventKind
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.mock_transcript_reader_test import ListTranscriptReader
from imbue.chat.harnesses.registry import get_model_state_path
from imbue.chat.harnesses.session import FileHarnessSession
from imbue.chat.harnesses.session import SendOutcome
from imbue.chat.message_stamps import MessageStampStore
from imbue.chat.models import AgentCreationError
from imbue.chat.models import AgentDestroyError
from imbue.chat.models import AgentNameConflictError
from imbue.chat.models import AgentRenameError
from imbue.chat.models import AgentStateItem
from imbue.chat.models import AgentStopError
from imbue.chat.models import ChatConvergingError
from imbue.chat.models import HandoffError
from imbue.chat.models import HandoffFailedStep
from imbue.chat.models import HandoffPhase
from imbue.chat.models import HeldSendOrigin
from imbue.chat.models import ModelPick
from imbue.chat.models import ProvisionalChat
from imbue.chat.models import ProvisionalChatPhase
from imbue.chat.models import QueuedMessageState
from imbue.chat.models import SummaryOutcome
from imbue.chat.models import TransitionKind
from imbue.chat.oom_prioritizer import ChatOomPrioritizer
from imbue.chat.presence import PresenceState
from imbue.chat.primitives import ChatId
from imbue.chat.testing import CONTINUE_CHAT_TEMPLATE_PATH
from imbue.chat.testing import RecordingShell
from imbue.chat.testing import make_chat_agent_entry
from imbue.chat.testing import make_chat_handoff_record
from imbue.chat.testing import make_chat_rebind_record
from imbue.chat.testing import make_two_member_chat_record
from imbue.chat.testing import seed_agent_state
from imbue.chat.testing import write_recording_mngr_binary
from imbue.chat.testing import write_summary_for_request
from imbue.chat.ws_broadcaster import WebSocketBroadcaster
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.imbue_common.model_update import to_update
from imbue.mngr.api.observe import make_agent_removed_event
from imbue.mngr.api.observe import make_agent_state_event
from imbue.mngr.api.observe import make_full_agent_state_event
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.interfaces.data_types import HostDetails
from imbue.mngr.primitives import AgentId as MngrAgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName as MngrAgentName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.polling import wait_for
from imbue.mngr_codex.app_server_client import CodexModel

# Several tests in this module spin up real watchdog FSEvents observers
# (the activity and model-state watchers). On macOS the FSEvents emitter thread
# occasionally stalls during shutdown, tripping pytest-timeout. Mark the
# whole file as flaky so offload retries it automatically -- mirrors
# ``ws_broadcaster_test.py``.
pytestmark = pytest.mark.flaky


def _seed_agent(
    manager: AgentManager,
    agent_id: str,
    harness: HarnessType = HarnessType.CLAUDE,
    state: str = "RUNNING",
) -> None:
    """Insert a placeholder ``AgentStateItem`` directly into the tracked map."""
    seed_agent_state(manager, agent_id, name=f"agent-{agent_id}", state=state, harness=harness)


_PROVIDER = ProviderInstanceName("local")


def _agent_details(
    name: str,
    agent_id: MngrAgentId | None = None,
    state: AgentLifecycleState = AgentLifecycleState.RUNNING,
    labels: dict[str, str] | None = None,
    work_dir: str = "/tmp/work",
    host_id: HostId | None = None,
    provider_name: ProviderInstanceName = _PROVIDER,
) -> AgentDetails:
    """Build an ``AgentDetails`` with controllable identity, state, and location.

    Mirrors what the observe stream carries: a real lifecycle ``state`` and a
    nested ``HostDetails`` whose id/provider are what ``_build_agent_match`` reads
    to route messages. Fields the manager never inspects are given inert defaults.
    """
    return AgentDetails(
        id=agent_id if agent_id is not None else MngrAgentId(),
        name=MngrAgentName(name),
        type="claude",
        command=CommandString("claude"),
        work_dir=Path(work_dir),
        initial_branch=None,
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=state,
        labels=labels if labels is not None else {},
        host=HostDetails(
            id=host_id if host_id is not None else HostId(),
            name="test-host",
            provider_name=provider_name,
            state=HostState.RUNNING,
        ),
    )


def _drain(q: queue.Queue[str | None]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    while not q.empty():
        raw = q.get_nowait()
        if raw is None:
            break
        out.append(json.loads(raw))
    return out


def _last_chats_updated(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for message in reversed(messages):
        if message.get("type") == "chats_updated":
            return message
    return None


@pytest.fixture(autouse=True)
def _signed_in_account() -> None:
    """One account, because creating a chat now requires one.

    There is no shared login to fall back to: `resolve_binding` raises rather than returning
    None, so a chat create with no account is refused. Autouse because every create in this
    module wants the ordinary case; the two that care about a SPECIFIC account mint their own
    and pass its id, and this one is simply not chosen.
    """
    account_id, _ = mint_account_dir()
    commit_account(account_id, "anthropic", "Anthropic")


def test_get_agents_initially_empty(agent_manager: AgentManager) -> None:
    agents = agent_manager.get_agents()
    assert agents == []


def test_get_provisional_chats_initially_empty(agent_manager: AgentManager) -> None:
    protos = agent_manager.get_provisional_chats()
    assert protos == []


@pytest.mark.parametrize("chat_ref", ["", "   "])
def test_a_blank_chat_ref_names_no_chat(agent_manager: AgentManager, chat_ref: str) -> None:
    """A route or instance key can hand the manager a blank id; that names nothing rather than
    tripping over the chat id primitive's own validation."""
    seed_agent_state(agent_manager, "agent-1", name="Chat-1")

    assert agent_manager.get_chat_snapshot(chat_ref) is None
    assert agent_manager.get_provisional_chat(chat_ref) is None
    assert agent_manager.discard_provisional_chat(chat_ref) is False


def test_get_chat_snapshots(agent_manager: AgentManager) -> None:
    with agent_manager._lock:
        agent_manager._agents["a1"] = AgentStateItem(
            id="a1",
            name="agent-one",
            state="RUNNING",
            labels={"user_created": "true"},
            work_dir="/tmp/work",
        )

    serialized = [snapshot.model_dump(mode="json") for snapshot in agent_manager.get_chat_snapshots()]
    assert len(serialized) == 1
    assert serialized[0]["chat_id"] == "a1"
    assert serialized[0]["name"] == "agent-one"
    assert serialized[0]["labels"] == {"user_created": "true"}
    assert serialized[0]["agent_ids"] == ["a1"]
    assert serialized[0]["handoff"] is None
    assert serialized[0]["active_agent"]["agent_id"] == "a1"
    assert serialized[0]["active_agent"]["activity_state"] is None


def test_resolve_agent_work_dir_from_own_env(agent_manager: AgentManager) -> None:
    with agent_manager._lock:
        result = agent_manager._resolve_agent_work_dir("test-agent-id")
    assert result == "/tmp/test-work"


def test_resolve_agent_work_dir_from_tracked_agent(agent_manager: AgentManager) -> None:
    with agent_manager._lock:
        agent_manager._agents["other-agent"] = AgentStateItem(
            id="other-agent",
            name="other",
            state="RUNNING",
            labels={},
            work_dir="/tmp/other-work",
        )
        result = agent_manager._resolve_agent_work_dir("other-agent")
    assert result == "/tmp/other-work"


def test_resolve_agent_work_dir_returns_none_for_unknown(agent_manager: AgentManager) -> None:
    with agent_manager._lock:
        result = agent_manager._resolve_agent_work_dir("unknown-id")
    assert result is None


def test_create_chat_broadcasts_provisional_chat_created(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster
) -> None:
    """The provisional_chat_created broadcast fires before the creation thread runs."""
    q = broadcaster.register()

    created = agent_manager.create_chat("test-chat")
    agent_manager.stop()

    assert isinstance(created.chat_id, str)
    assert len(created.chat_id) > 0
    assert created.name == "test-chat"
    assert created.display_name == "test-chat"

    raw = q.get_nowait()
    assert raw is not None
    proto_msg = json.loads(raw)
    assert proto_msg["type"] == "provisional_chat_created"
    assert proto_msg["chat_id"] == created.chat_id
    assert proto_msg["name"] == "test-chat"
    assert proto_msg["phase"] == "creating"


def test_create_codex_chat_broadcasts_provisional_chat_created_with_its_account(
    agent_manager: AgentManager,
    broadcaster: WebSocketBroadcaster,
    git_work_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The proto message names the account the chat launches on, so a retry can reuse it."""
    q = broadcaster.register()

    with agent_manager._lock:
        agent_manager._agents[agent_manager._own_agent_id] = AgentStateItem(
            id=agent_manager._own_agent_id,
            name="primary",
            state="RUNNING",
            labels={},
            work_dir=str(git_work_dir),
        )

    codex_account_id, _ = mint_account_dir()
    commit_account(codex_account_id, "openai", "OpenAI")
    created = agent_manager.create_chat("test-codex", account_id=codex_account_id)
    agent_manager.stop()

    assert isinstance(created.chat_id, str)

    raw = q.get_nowait()
    assert raw is not None
    proto_msg = json.loads(raw)
    assert proto_msg["type"] == "provisional_chat_created"
    assert proto_msg["phase"] == "creating"
    assert proto_msg["account_id"] == codex_account_id


def test_reserve_chat_mints_a_chat_awaiting_an_account(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster
) -> None:
    """A reservation is a provisional chat in the awaiting-account phase, pushed like a launch."""
    q = broadcaster.register()

    reserved = agent_manager.reserve_chat()

    proto = agent_manager.get_provisional_chat(reserved.chat_id)
    assert proto is not None
    assert proto.phase is ProvisionalChatPhase.AWAITING_ACCOUNT
    assert proto.name == reserved.display_name == "Chat 1"
    raw = q.get_nowait()
    assert raw is not None
    assert json.loads(raw) == {"type": "provisional_chat_created", **proto.model_dump(mode="json")}


def test_create_chat_launches_a_reserved_chat_under_its_id_and_name(agent_manager: AgentManager) -> None:
    reserved = agent_manager.reserve_chat()
    _tracked_chat(agent_manager, "agent-2", "Chat-2", display_name="Chat 2")

    created = agent_manager.create_chat("", chat_id=reserved.chat_id)
    agent_manager.stop()

    assert created.chat_id == reserved.chat_id
    assert created.display_name == reserved.display_name


def test_create_chat_refuses_launching_an_id_it_did_not_reserve(agent_manager: AgentManager) -> None:
    with agent_manager._lock:
        agent_manager._provisional_chats[ChatId("proto-1")] = ProvisionalChat(
            chat_id=ChatId("proto-1"), name="Chat 1", phase=ProvisionalChatPhase.CREATING
        )
    with pytest.raises(AgentCreationError):
        agent_manager.create_chat("", chat_id="never-reserved")
    with pytest.raises(AgentCreationError):
        agent_manager.create_chat("", chat_id="proto-1")
    agent_manager.stop()


def test_create_chat_refuses_a_name_or_project_beside_a_reserved_id(agent_manager: AgentManager) -> None:
    """A chat minted earlier keeps the name and project it was minted with: a launch that names either
    is refused rather than answered with a different name than it asked for."""
    reserved = agent_manager.reserve_chat()
    with pytest.raises(AgentCreationError, match="keeps the name and project"):
        agent_manager.create_chat("Renamed", chat_id=reserved.chat_id)
    with pytest.raises(AgentCreationError, match="keeps the name and project"):
        agent_manager.create_chat("", project_id="project-1", chat_id=reserved.chat_id)
    reserved_proto = agent_manager.get_provisional_chat(reserved.chat_id)
    assert reserved_proto is not None
    assert reserved_proto.phase is ProvisionalChatPhase.AWAITING_ACCOUNT
    agent_manager.stop()


def test_create_chat_refuses_a_message_beside_a_reserved_id(agent_manager: AgentManager) -> None:
    """A reserved chat keeps the first message it was minted with; a launch cannot reseed it."""
    reserved = agent_manager.reserve_chat(message="/welcome-tour")
    with pytest.raises(AgentCreationError, match="first message"):
        agent_manager.create_chat("", chat_id=reserved.chat_id, message="something else")
    reserved_proto = agent_manager.get_provisional_chat(reserved.chat_id)
    assert reserved_proto is not None
    assert reserved_proto.message == "/welcome-tour"
    assert reserved_proto.phase is ProvisionalChatPhase.AWAITING_ACCOUNT
    agent_manager.stop()


def test_a_chat_created_with_a_message_starts_on_it_rather_than_on_welcome(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster
) -> None:
    """A chat created with its own first message carries it; ``/welcome`` is only for a chat
    that starts with nothing to say (``launch_role_templates``)."""
    q = broadcaster.register()

    seeded = agent_manager.create_chat("seeded-chat", message="Teach me about Mind")
    agent_manager.stop()

    raw = q.get_nowait()
    assert raw is not None
    proto_msg = json.loads(raw)
    assert proto_msg["chat_id"] == seeded.chat_id
    assert proto_msg["message"] == "Teach me about Mind"


@pytest.mark.parametrize(
    ("message", "is_fast", "expected"),
    [
        ("", True, ("welcome", "fast")),
        ("", False, ("welcome",)),
        ("Teach me about Mind", True, ("fast",)),
        ("Teach me about Mind", False, ()),
    ],
)
def test_launch_role_templates_follow_the_message_and_the_chats_fast_mode(
    message: str, is_fast: bool, expected: tuple[str, ...]
) -> None:
    """Every chat that starts silent is greeted; a chat starts fast when its fast mode calls for it."""
    assert launch_role_templates(message, is_fast) == expected


def test_a_new_chat_takes_the_workspaces_default_fast_mode_and_keeps_it_in_its_folder(
    broadcaster: WebSocketBroadcaster, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default decides the launch and is written as the chat's own mode, so a later change to
    the default leaves this chat where it was."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(tmp_path))
    mngr_binary, argv_log = write_recording_mngr_binary(tmp_path)
    settings = ChatSettingsStore(path=None)
    settings.write(ChatSettings(fast_mode_default=FastModeMode.OFF))
    manager = AgentManager.build(
        broadcaster, mngr_binary=mngr_binary, chat_files_root=tmp_path / "chats", chat_settings=settings
    )
    try:
        created = manager.create_chat("", message="hello")
        assert wait_until(lambda: manager.get_provisional_chat(created.chat_id) is None, timeout_seconds=10)
    finally:
        manager.stop()

    (argv_line,) = argv_log.read_text().splitlines()
    assert "--template fast" not in argv_line
    assert read_fast_mode_state(tmp_path / "chats" / created.chat_id) == ChatFastModeState(mode=FastModeMode.OFF)
    assert manager.get_fast_mode_state(ChatId(created.chat_id)).mode is FastModeMode.OFF


def test_a_handoffs_successor_starts_fast_only_when_the_chats_mode_calls_for_it(
    broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    manager = AgentManager.build(broadcaster, chat_files_root=tmp_path / "chats")
    try:
        chat_id = ChatId("agent-fastchat")
        spec = SuccessorCreateSpec(
            name="Chat 1",
            chat_id=chat_id,
            agent_id="agent-next",
            harness=HarnessType.CLAUDE,
            project_id="",
            account_id="acct-1",
            extra_labels=(),
        )
        # No mode of its own: the workspace default (auto) launches fast.
        assert "fast" in manager._build_successor_create_command(spec)
        manager.set_fast_mode_state(chat_id, ChatFastModeState(mode=FastModeMode.AUTO, is_switched=True))
        assert "fast" not in manager._build_successor_create_command(spec)
        manager.set_fast_mode_state(chat_id, ChatFastModeState(mode=FastModeMode.ON))
        assert "fast" in manager._build_successor_create_command(spec)
    finally:
        manager.stop()


def _seed_turns() -> tuple[SeedTurn, ...]:
    return (
        SeedTurn(role=SeedRole.USER, text="Wait.. what is honest software?"),
        SeedTurn(role=SeedRole.ASSISTANT, text="Software that works for you."),
        SeedTurn(role=SeedRole.ASSISTANT, text="Your workspace is ready! How would you like to start?"),
    )


def _seed_files_root(tmp_path: Path) -> Path:
    """Where a seeded test's manager keeps its chats' seed files."""
    return tmp_path / "chats"


def _seed_manager(
    broadcaster: WebSocketBroadcaster,
    tmp_path: Path,
    store: InMemoryChatRecordStore | None = None,
    mngr_binary: str | None = None,
    auto_open: AutoOpenReactor | None = None,
) -> tuple[AgentManager, InMemoryChatRecordStore]:
    """A manager for the seeded-chat tests: over ``store`` (a fresh one when None), its seed files
    under ``tmp_path``, and the manager's own ``mngr`` unless a recording one is given."""
    store = store if store is not None else InMemoryChatRecordStore()
    manager = AgentManager.build(
        broadcaster,
        mngr_binary=mngr_binary if mngr_binary is not None else "mngr",
        auto_open=auto_open,
        chat_record_store=store,
        chat_files_root=_seed_files_root(tmp_path),
    )
    return manager, store


def test_seed_chat_opens_a_provisional_chat_awaiting_its_first_send_on_the_seeded_turns(
    broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """The Mind app's conversation becomes the chat's first segment: the record names the seed
    as its only member, the seed file holds the turns, and the chat is listed awaiting the user."""
    manager, store = _seed_manager(broadcaster, tmp_path)
    q = broadcaster.register()
    try:
        created = manager.seed_chat("Getting started", _seed_turns())

        chat_id = ChatId(created.chat_id)
        assert created.display_name == "Getting started"
        provisional = manager.get_provisional_chat(created.chat_id)
        assert provisional is not None
        assert provisional.phase is ProvisionalChatPhase.AWAITING_FIRST_SEND
        assert provisional.is_seeded is True
        record = store.read(chat_id)
        assert record is not None
        assert record.is_seed_only and record.seed_title == "Getting started"
        assert record.agents[0].final_event_count == 3
        events = read_seed_events(_seed_files_root(tmp_path) / chat_id)
        assert [event["type"] for event in events] == ["user_message", "assistant_message", "assistant_message"]
        assert events[0]["event_id"] == seed_event_id(chat_id, 0)
        # The seed reads back as the chat's one (ended) segment.
        segments = manager.get_chat_segments(chat_id)
        assert segments is not None
        (segment,) = segments
        assert segment.agent.harness is HarnessType.SEED and segment.is_active is False
        raw = q.get_nowait()
        assert raw is not None
        broadcast = json.loads(raw)
        assert broadcast["type"] == "provisional_chat_created"
        assert broadcast["chat_id"] == created.chat_id
    finally:
        manager.stop()


def test_a_seeded_chat_is_launched_by_its_first_send_as_the_seeds_successor(
    broadcaster: WebSocketBroadcaster, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user's first message launches the chat's first real agent: a fresh id under the
    chat's, joining the record as its second member, with the membership labels a handoff's
    successor carries, the message it was sent, and no ``/welcome``."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(tmp_path))
    mngr_binary, argv_log = write_recording_mngr_binary(tmp_path)
    manager, store = _seed_manager(broadcaster, tmp_path, mngr_binary=mngr_binary)
    try:
        seeded = manager.seed_chat("Getting started", _seed_turns())
        launched = manager.create_chat("", chat_id=seeded.chat_id, message="Let's build something")
        # The create runs on a thread; stopping the manager before it lands would kill the fake mngr.
        assert wait_until(lambda: manager.get_provisional_chat(seeded.chat_id) is None, timeout_seconds=10)
    finally:
        manager.stop()

    assert launched.chat_id == seeded.chat_id
    assert launched.display_name == "Getting started"
    assert manager.get_provisional_chat(seeded.chat_id) is None
    record = store.read(ChatId(seeded.chat_id))
    assert record is not None
    seed, agent = record.agents
    assert agent.seq == 2 and agent.agent_id != seeded.chat_id
    assert agent.harness is HarnessType.CLAUDE and agent.ended_at is None
    assert manager.get_agent_by_id(agent.agent_id) is not None
    (argv_line,) = argv_log.read_text().splitlines()
    argv = argv_line.split()
    templates = [argv[i + 1] for i, tok in enumerate(argv) if tok == "--template"]
    assert templates == ["chat", "fast"]
    assert f"chat_id={seeded.chat_id}" in argv and "chat_seq=2" in argv
    assert "Let's" in argv_line and "/welcome" not in argv_line


def test_a_seeded_chat_whose_launch_failed_is_relaunched_as_the_seeds_successor(
    broadcaster: WebSocketBroadcaster, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The page's "Try again" on a seeded chat whose first launch failed: the retry keeps the
    first send it was launched with and, like the first launch, gives the agent a fresh id and a
    place on the record after the seed, so the chat resolves to it once the create lands."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(tmp_path))
    mngr_binary, argv_log = write_recording_mngr_binary(tmp_path)
    manager, store = _seed_manager(broadcaster, tmp_path, mngr_binary=mngr_binary)
    (signed_in,) = read_index().accounts
    try:
        seeded = manager.seed_chat("Getting started", _seed_turns())
        chat_id = ChatId(seeded.chat_id)
        # The first launch failed: what _run_creation leaves behind when mngr create exits non-zero.
        with manager._lock:
            awaiting = manager._provisional_chats[chat_id]
            manager._provisional_chats[chat_id] = awaiting.model_copy_update(
                to_update(awaiting.field_ref().account_id, signed_in.id),
                to_update(awaiting.field_ref().message, "Let's build something"),
                to_update(awaiting.field_ref().phase, ProvisionalChatPhase.FAILED),
                to_update(awaiting.field_ref().error, "mngr create exited with code 3"),
            )

        with pytest.raises(AgentCreationError, match="keeps the first message"):
            manager.create_chat("", chat_id=seeded.chat_id, account_id=signed_in.id, message="Something else")
        relaunched = manager.create_chat("", chat_id=seeded.chat_id, account_id=signed_in.id)
        assert wait_until(lambda: manager.get_provisional_chat(seeded.chat_id) is None, timeout_seconds=10)

        assert relaunched.chat_id == seeded.chat_id
        record = store.read(chat_id)
        assert record is not None
        seed, agent = record.agents
        assert agent.seq == 2 and agent.agent_id != seeded.chat_id and agent.ended_at is None
        active = manager.get_active_agent_info(chat_id)
        assert active is not None and active.id == agent.agent_id
        (argv_line,) = argv_log.read_text().splitlines()
        assert f"chat_id={seeded.chat_id}" in argv_line.split() and "chat_seq=2" in argv_line.split()
        assert "Let's" in argv_line and "Something else" not in argv_line
    finally:
        manager.stop()


def _write_gated_mngr_binary(tmp_path: Path, create_exit_code: int = 0) -> tuple[str, Path, Path]:
    """A stand-in ``mngr`` whose create blocks until the returned go-file exists, so the state a
    create leaves while it runs is observable, then exits ``create_exit_code``; every other
    command succeeds at once. Every argv is appended to the returned log."""
    go_path = tmp_path / "mngr-go"
    log_path = tmp_path / "mngr-argv.log"
    script = tmp_path / "fake-mngr"
    script.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log_path}"\n'
        f'if [ "$1" = "create" ]; then while [ ! -e "{go_path}" ]; do sleep 0.05; done; exit {create_exit_code}; fi\n'
        "exit 0\n"
    )
    script.chmod(0o755)
    return str(script), go_path, log_path


def test_a_seeded_chats_first_agent_is_the_chats_from_its_create_on_and_never_a_chat_of_its_own(
    broadcaster: WebSocketBroadcaster, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """mngr lists an agent as soon as it is provisioned, seconds before its create returns, and an
    agent no record names is a chat of its own: the record names the seeded chat's agent from before
    the create, as a handoff names its successor, so that listing puts the chat under the seed's id
    rather than a second chat beside it."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(tmp_path))
    mngr_binary, go_path, _argv_log = _write_gated_mngr_binary(tmp_path)
    manager, store = _seed_manager(broadcaster, tmp_path, mngr_binary=mngr_binary)
    try:
        seeded = manager.seed_chat("Getting started", _seed_turns())
        chat_id = ChatId(seeded.chat_id)
        manager.create_chat("", chat_id=seeded.chat_id, message="Let's build something")

        # The create is still running: the record already names the agent as the chat's, and
        # the seed is the whole transcript until mngr lists it.
        record = store.read(chat_id)
        assert record is not None
        _seed, agent = record.agents
        assert agent.seq == 2 and agent.ended_at is None
        assert manager.chat_id_of_agent(agent.agent_id) == chat_id
        assert manager.get_provisional_chat(seeded.chat_id) is not None
        assert manager.get_chat_snapshots() == []
        seed_segments = manager.get_chat_segments(chat_id)
        assert seed_segments is not None
        (seed_segment,) = seed_segments
        assert seed_segment.agent.harness is HarnessType.SEED

        # mngr lists the agent mid-create: one chat, under the seed's id, reading both segments.
        listed = _agent_details(
            "Getting-started",
            agent_id=MngrAgentId(agent.agent_id),
            labels={"user_created": "true", "chat_id": seeded.chat_id, "chat_seq": "2"},
        )
        manager._handle_observe_event(make_agent_state_event(listed))
        assert [snapshot.chat_id for snapshot in manager.get_chat_snapshots()] == [chat_id]
        segments = manager.get_chat_segments(chat_id)
        assert segments is not None
        assert [segment.agent.harness for segment in segments] == [HarnessType.SEED, HarnessType.CLAUDE]

        go_path.touch()
        assert wait_until(lambda: manager.get_provisional_chat(seeded.chat_id) is None, timeout_seconds=10)
        landed = store.read(chat_id)
        assert landed is not None
        assert [entry.agent_id for entry in landed.agents] == [seeded.chat_id, agent.agent_id]
    finally:
        go_path.touch()
        manager.stop()


def test_a_seeded_chats_failed_create_takes_its_agent_back_off_the_record(
    broadcaster: WebSocketBroadcaster, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A create that fails leaves the chat seed-only again, failed and still seeded, so the page's
    "Try again" and its discard find the chat as it was before the launch."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(tmp_path))
    script = tmp_path / "fake-mngr"
    script.write_text('#!/bin/sh\necho "No provider account is signed in" >&2\nexit 3\n')
    script.chmod(0o755)
    manager, store = _seed_manager(broadcaster, tmp_path, mngr_binary=str(script))

    def is_failed() -> bool:
        provisional = manager.get_provisional_chat(seeded.chat_id)
        return provisional is not None and provisional.phase is ProvisionalChatPhase.FAILED

    try:
        seeded = manager.seed_chat("Getting started", _seed_turns())
        chat_id = ChatId(seeded.chat_id)
        manager.create_chat("", chat_id=seeded.chat_id, message="Let's build something")
        assert wait_until(is_failed, timeout_seconds=10)

        failed = manager.get_provisional_chat(seeded.chat_id)
        assert failed is not None
        assert failed.is_seeded and failed.message == "Let's build something"
        record = store.read(chat_id)
        assert record is not None and record.is_seed_only
        assert manager.discard_provisional_chat(seeded.chat_id) is True
        assert store.read(chat_id) is None
    finally:
        manager.stop()


def test_a_seeded_chats_failed_create_destroys_the_agent_mngr_had_already_made(
    broadcaster: WebSocketBroadcaster, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """mngr lists an agent before its create returns, so a create that fails after provisioning
    leaves one behind that the withdrawn record no longer names: it would be listed as a chat of
    its own, and the retry mints a fresh id, so the failure drops and destroys it, as a handoff's
    retry does with its half-made successor."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(tmp_path))
    mngr_binary, go_path, argv_log = _write_gated_mngr_binary(tmp_path, create_exit_code=3)
    manager, store = _seed_manager(broadcaster, tmp_path, mngr_binary=mngr_binary)

    def destroys() -> list[str]:
        # The log exists once the fake mngr has run at all, which the create's thread may not have got to yet.
        if not argv_log.exists():
            return []
        return [line for line in argv_log.read_text().splitlines() if line.startswith("destroy ")]

    try:
        seeded = manager.seed_chat("Getting started", _seed_turns())
        chat_id = ChatId(seeded.chat_id)
        manager.create_chat("", chat_id=seeded.chat_id, message="Let's build something")
        record = store.read(chat_id)
        assert record is not None
        _seed, agent = record.agents
        listed = _agent_details(
            "Getting-started",
            agent_id=MngrAgentId(agent.agent_id),
            labels={"user_created": "true", "chat_id": seeded.chat_id, "chat_seq": "2"},
        )
        manager._handle_observe_event(make_agent_state_event(listed))
        assert manager.get_agent_by_id(agent.agent_id) is not None

        go_path.touch()
        assert wait_until(lambda: len(destroys()) == 1, timeout_seconds=10)

        assert destroys() == [f"destroy {agent.agent_id} --force"]
        failed = manager.get_provisional_chat(seeded.chat_id)
        assert failed is not None and failed.phase is ProvisionalChatPhase.FAILED
        withdrawn = store.read(chat_id)
        assert withdrawn is not None and withdrawn.is_seed_only
        # Neither the seeded chat (provisional, no agent) nor the orphan is a listed chat.
        assert manager.get_agent_by_id(agent.agent_id) is None
        assert manager.get_chat_snapshots() == []
    finally:
        go_path.touch()
        manager.stop()


def test_seed_chat_checks_its_title_like_a_launch_checks_a_requested_name(
    broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """A title the first send's ``mngr create`` could not use is refused here, with nothing seeded."""
    manager, store = _seed_manager(broadcaster, tmp_path)
    try:
        with pytest.raises(AgentCreationError, match="no usable characters"):
            manager.seed_chat("!!!", _seed_turns())
        manager.seed_chat("Getting started", _seed_turns())
        with pytest.raises(AgentNameConflictError, match="already exists"):
            manager.seed_chat("getting-started", _seed_turns())
        assert [proto.name for proto in manager.get_provisional_chats()] == ["Getting started"]
        assert len(store.read_all()) == 1
    finally:
        manager.stop()


def test_a_seeded_chat_must_be_launched_with_a_message(
    broadcaster: WebSocketBroadcaster, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The launch resolves its work dir before it reads the message, so name one (CI sets none).
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(tmp_path))
    manager, _store = _seed_manager(broadcaster, tmp_path)
    try:
        seeded = manager.seed_chat("", _seed_turns())
        with pytest.raises(AgentCreationError, match="first message"):
            manager.create_chat("", chat_id=seeded.chat_id)
        provisional = manager.get_provisional_chat(seeded.chat_id)
        assert provisional is not None
        assert provisional.phase is ProvisionalChatPhase.AWAITING_FIRST_SEND
    finally:
        manager.stop()


def test_discarding_a_seeded_chat_drops_its_record_with_it(broadcaster: WebSocketBroadcaster, tmp_path: Path) -> None:
    manager, store = _seed_manager(broadcaster, tmp_path)
    try:
        seeded = manager.seed_chat("", _seed_turns())
        assert manager.discard_provisional_chat(seeded.chat_id) is True
        assert manager.get_provisional_chat(seeded.chat_id) is None
        assert store.read(ChatId(seeded.chat_id)) is None
        assert manager.get_chat_segments(ChatId(seeded.chat_id)) is None
    finally:
        manager.stop()


def test_a_build_restores_the_seeded_chats_still_awaiting_their_first_send(
    broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """The seed's record is on disk, so a restart of this app lists the chat again, awaiting the user."""
    first, store = _seed_manager(broadcaster, tmp_path)
    try:
        seeded = first.seed_chat("Getting started", _seed_turns())
    finally:
        first.stop()

    reactor = AutoOpenReactor(ledger=AutoOpenLedger(path=None), shell=RecordingShell(client_ids=[]))
    second, _ = _seed_manager(broadcaster, tmp_path, store=store, auto_open=reactor)
    try:
        restored = second.get_provisional_chat(seeded.chat_id)
        assert restored is not None
        assert restored.phase is ProvisionalChatPhase.AWAITING_FIRST_SEND
        assert restored.name == "Getting started" and restored.is_seeded is True
        # Its tab is still owed: nobody was connected to see it before the restart.
        assert reactor.pending_chat_ids() == {ChatId(seeded.chat_id)}
    finally:
        second.stop()

    # A tab the ledger says was delivered is not popped again by a restart.
    delivered_ledger = AutoOpenLedger(path=None)
    delivered_ledger.mark_delivered(ChatId(seeded.chat_id))
    delivered_reactor = AutoOpenReactor(ledger=delivered_ledger, shell=RecordingShell(client_ids=[]))
    third, _ = _seed_manager(broadcaster, tmp_path, store=store, auto_open=delivered_reactor)
    try:
        assert third.get_provisional_chat(seeded.chat_id) is not None
        assert delivered_reactor.pending_chat_ids() == set()
    finally:
        third.stop()


def _seed_failed_chat(
    agent_manager: AgentManager, chat_id: ChatId, name: str, account_id: str = "acct-1"
) -> ProvisionalChat:
    proto = ProvisionalChat(
        chat_id=chat_id,
        name=name,
        account_id=account_id,
        phase=ProvisionalChatPhase.FAILED,
        error="mngr create exited with code 3",
    )
    with agent_manager._lock:
        agent_manager._provisional_chats[chat_id] = proto
    return proto


def test_create_chat_relaunches_a_failed_chat_under_its_id_and_name(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster
) -> None:
    """The page's "Try again": the failed record is launched again on its account, keeping the id
    the tab was docked under and the name it was minted with, with its reason cleared. The
    record is read off the push the launch sends before its create thread runs (the fake
    ``mngr`` of this fixture fails at once, which would settle it again)."""
    # The account the conftest signed in: what the failed record binds to and the retry names.
    (signed_in,) = read_index().accounts
    failed = _seed_failed_chat(agent_manager, ChatId("failed-1"), "Chat 1", account_id=signed_in.id)
    q = broadcaster.register()

    created = agent_manager.create_chat("", chat_id="failed-1", account_id=failed.account_id)
    agent_manager.stop()

    assert created.chat_id == "failed-1"
    assert created.display_name == "Chat 1"
    raw = q.get_nowait()
    assert raw is not None
    assert json.loads(raw) == {
        "type": "provisional_chat_created",
        "chat_id": "failed-1",
        "name": "Chat 1",
        "project_id": "",
        "account_id": signed_in.id,
        "message": "",
        "phase": "creating",
        "error": None,
        "is_seeded": False,
    }
    assert [proto.chat_id for proto in agent_manager.get_provisional_chats()] == ["failed-1"]


def test_discard_provisional_chat_drops_a_failed_chat(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster
) -> None:
    _seed_failed_chat(agent_manager, ChatId("failed-1"), "Chat 1")
    q = broadcaster.register()

    assert agent_manager.discard_provisional_chat("failed-1") is True

    assert agent_manager.get_provisional_chat("failed-1") is None
    raw = q.get_nowait()
    assert raw is not None
    assert json.loads(raw) == {
        "type": "provisional_chat_completed",
        "chat_id": "failed-1",
        "success": False,
        "error": None,
    }


def test_discard_provisional_chat_drops_a_reserved_chat_but_not_a_create_in_flight(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster
) -> None:
    reserved = agent_manager.reserve_chat()
    with agent_manager._lock:
        agent_manager._provisional_chats[ChatId("proto-1")] = ProvisionalChat(
            chat_id=ChatId("proto-1"), name="Chat 9", phase=ProvisionalChatPhase.CREATING
        )
    q = broadcaster.register()

    assert agent_manager.discard_provisional_chat(reserved.chat_id) is True
    assert agent_manager.discard_provisional_chat("proto-1") is False
    assert agent_manager.discard_provisional_chat("never-minted") is False

    assert [proto.chat_id for proto in agent_manager.get_provisional_chats()] == ["proto-1"]
    raw = q.get_nowait()
    assert raw is not None
    assert json.loads(raw) == {
        "type": "provisional_chat_completed",
        "chat_id": reserved.chat_id,
        "success": False,
        "error": None,
    }


def test_stop_without_start(agent_manager: AgentManager) -> None:
    """Stopping an agent manager that was never started is safe."""
    agent_manager.stop()


def test_agent_state_event_adds_agent(agent_manager: AgentManager, broadcaster: WebSocketBroadcaster) -> None:
    """An AGENT_STATE event for a new agent updates the agent list and broadcasts."""
    q = broadcaster.register()

    test_agent_id = MngrAgentId()
    agent = _agent_details("discovered-agent", agent_id=test_agent_id, labels={"user_created": "true"})

    agent_manager._handle_observe_event(make_agent_state_event(agent))

    agents = agent_manager.get_agents()
    assert len(agents) == 1
    assert agents[0].id == str(test_agent_id)
    assert agents[0].name == "discovered-agent"

    raw = q.get_nowait()
    assert raw is not None
    msg = json.loads(raw)
    assert msg["type"] == "chats_updated"


def test_agent_removed_event_removes_agent(agent_manager: AgentManager, broadcaster: WebSocketBroadcaster) -> None:
    """An AGENT_REMOVED event removes the agent from the tracked list and broadcasts."""
    test_agent_id = MngrAgentId()
    str_id = str(test_agent_id)
    q = broadcaster.register()

    agent = _agent_details("doomed", agent_id=test_agent_id)
    agent_manager._handle_observe_event(make_agent_state_event(agent))
    assert len(agent_manager.get_agents()) == 1

    q.get_nowait()

    agent_manager._handle_observe_event(make_agent_removed_event(agent.id, agent.name, agent.host.id))

    agents = agent_manager.get_agents()
    assert len(agents) == 0

    raw = q.get_nowait()
    assert raw is not None
    msg = json.loads(raw)
    assert msg["type"] == "chats_updated"
    assert str_id not in [chat["chat_id"] for chat in msg["chats"]]


def _full_snapshot_with_agent(name: str) -> tuple[MngrAgentId, HostId, AgentDetails]:
    agent = _agent_details(name)
    return agent.id, agent.host.id, agent


def test_full_snapshot_populates_agent_locations(agent_manager: AgentManager) -> None:
    """A snapshot records each agent's routing location (id/host/provider) so messaging skips discovery."""
    agent_id, host_id, agent = _full_snapshot_with_agent("locatable")
    agent_manager._handle_observe_event(make_full_agent_state_event([agent]))

    matches = agent_manager.get_agent_matches_by_id(str(agent_id))
    assert len(matches) == 1
    match = matches[0]
    assert str(match.agent_id) == str(agent_id)
    assert str(match.agent_name) == "locatable"
    assert str(match.host_id) == str(host_id)
    assert str(match.provider_name) == "local"

    assert agent_manager.get_agent_matches_by_id("agent-does-not-exist") == []


def test_agent_location_dropped_when_absent_from_snapshot(agent_manager: AgentManager) -> None:
    """An agent missing from a later snapshot loses its cached location."""
    agent_id, _host_id, agent = _full_snapshot_with_agent("ephemeral")
    agent_manager._handle_observe_event(make_full_agent_state_event([agent]))
    assert len(agent_manager.get_agent_matches_by_id(str(agent_id))) == 1

    agent_manager._handle_observe_event(make_full_agent_state_event([]))
    assert agent_manager.get_agent_matches_by_id(str(agent_id)) == []


def test_get_agent_matches_by_id_disambiguates_shared_name(agent_manager: AgentManager) -> None:
    """Two agents sharing a name on different hosts are each retrievable by their own id."""
    host_a, host_b = HostId(), HostId()
    agent_a = _agent_details("twin", host_id=host_a)
    agent_b = _agent_details("twin", host_id=host_b)
    agent_manager._handle_observe_event(make_full_agent_state_event([agent_a, agent_b]))

    matches_a = agent_manager.get_agent_matches_by_id(str(agent_a.id))
    matches_b = agent_manager.get_agent_matches_by_id(str(agent_b.id))
    assert len(matches_a) == 1 and str(matches_a[0].host_id) == str(host_a)
    assert len(matches_b) == 1 and str(matches_b[0].host_id) == str(host_b)


def test_agent_location_updates_when_host_changes(agent_manager: AgentManager) -> None:
    """A later snapshot relocating an agent (new host_id) replaces its cached location."""
    agent_id = MngrAgentId()
    host_a, host_b = HostId(), HostId()
    agent_manager._handle_observe_event(
        make_full_agent_state_event([_agent_details("mover", agent_id=agent_id, host_id=host_a)])
    )
    assert str(agent_manager.get_agent_matches_by_id(str(agent_id))[0].host_id) == str(host_a)

    agent_manager._handle_observe_event(
        make_full_agent_state_event([_agent_details("mover", agent_id=agent_id, host_id=host_b)])
    )
    matches = agent_manager.get_agent_matches_by_id(str(agent_id))
    assert len(matches) == 1
    assert str(matches[0].host_id) == str(host_b)


def test_remove_agent_drops_location(agent_manager: AgentManager) -> None:
    """remove_agent (the API destroy path) drops the cached location too."""
    agent_id, _host_id, agent = _full_snapshot_with_agent("doomed")
    agent_manager._handle_observe_event(make_full_agent_state_event([agent]))
    assert len(agent_manager.get_agent_matches_by_id(str(agent_id))) == 1

    agent_manager.remove_agent(str(agent_id))
    assert agent_manager.get_agent_matches_by_id(str(agent_id)) == []


def test_get_agent_info_by_id_resolves_from_state(agent_manager: AgentManager, tmp_path: Path) -> None:
    """get_agent_info_by_id builds an AgentInfo from the live state (with resolved dirs)."""
    with agent_manager._lock:
        agent_manager._agents["agent-1"] = AgentStateItem(
            id="agent-1", name="alpha", state="RUNNING", labels={"k": "v"}, work_dir="/w"
        )

    info = agent_manager.get_agent_info_by_id("agent-1")
    assert info is not None
    assert info.id == "agent-1"
    assert info.name == "alpha"
    assert info.labels == {"k": "v"}
    assert agent_manager.get_agent_info_by_id("missing") is None


def test_agent_state_event_locates_agent_immediately(agent_manager: AgentManager) -> None:
    """An AGENT_STATE event records the routing location (id/host/provider) at once,
    so the first message to a just-created agent skips discovery instead of waiting for
    the next full snapshot."""
    fresh = _agent_details("freshly-created")
    agent_manager._handle_observe_event(make_agent_state_event(fresh))

    matches = agent_manager.get_agent_matches_by_id(str(fresh.id))
    assert len(matches) == 1
    assert str(matches[0].agent_name) == "freshly-created"
    assert str(matches[0].host_id) == str(fresh.host.id)
    assert str(matches[0].provider_name) == "local"


def test_unknown_observe_event_type_is_ignored(agent_manager: AgentManager) -> None:
    """An observe line whose ``type`` is not one of the three agents-stream events is ignored.

    ``parse_observe_event_line`` returns None for unrecognized (forward-compatible)
    types, so the output-line handler must swallow it without raising or mutating
    the tracked agent set.
    """
    line = json.dumps(
        {
            "type": "AGENT_STATE_CHANGE",
            "timestamp": "2026-01-01T00:00:00.000000000Z",
            "event_id": "test-event-id",
            "source": "mngr/agent_states",
        }
    )
    agent_manager._handle_observe_output_line(line, True)
    assert agent_manager.get_agents() == []


def test_create_chat_raises_when_the_primary_work_dir_is_unknown(agent_manager: AgentManager) -> None:
    """A chat has nowhere to be created if the primary's work dir cannot be resolved.

    Both the registered agent and the own-work-dir fallback must be absent for the
    guard to bite, so clear the fallback the fixture provides.
    """
    with agent_manager._lock:
        agent_manager._agents.pop(agent_manager._own_agent_id, None)
        # Empty is the unset form: it is what the manager starts with when
        # MNGR_AGENT_WORK_DIR is absent, and the fallback treats it as falsy.
        agent_manager._own_work_dir = ""
    with pytest.raises(AgentCreationError, match="Cannot determine work directory"):
        agent_manager.create_chat("test")


def test_initial_discover_populates_agents(
    broadcaster: WebSocketBroadcaster,
) -> None:
    """Initial discovery populates agent list when discovery succeeds."""
    manager = AgentManager.build(broadcaster)
    manager._initial_discover()


def test_initial_discover_handles_errors(
    broadcaster: WebSocketBroadcaster,
) -> None:
    """Initial discovery handles errors gracefully when mngr is unavailable."""
    manager = AgentManager.build(broadcaster)
    manager._initial_discover()
    assert isinstance(manager.get_agents(), list)


def test_refresh_agents_does_not_crash(agent_manager: AgentManager, broadcaster: WebSocketBroadcaster) -> None:
    """Refresh agents handles errors gracefully and does not raise."""
    agent_manager._refresh_agents()
    assert isinstance(agent_manager.get_agents(), list)


def test_full_snapshot_replaces_agent_set(agent_manager: AgentManager, broadcaster: WebSocketBroadcaster) -> None:
    """A full state snapshot replaces the entire tracked agent set."""
    q = broadcaster.register()

    agent1 = _agent_details("agent-one", work_dir="/tmp/w1")
    agent2 = _agent_details("agent-two", work_dir="/tmp/w2")
    event = make_full_agent_state_event([agent1, agent2])

    agent_manager._handle_observe_event(event)

    agents = agent_manager.get_agents()
    assert len(agents) == 2

    raw = q.get_nowait()
    assert raw is not None
    msg = json.loads(raw)
    assert msg["type"] == "chats_updated"
    assert len(msg["chats"]) == 2


def _seed_creating_chat(agent_manager: AgentManager, chat_id: ChatId, name: str) -> None:
    with agent_manager._lock:
        agent_manager._provisional_chats[chat_id] = ProvisionalChat(
            chat_id=chat_id, name=name, account_id="acct-1", phase=ProvisionalChatPhase.CREATING
        )


def test_run_creation_registers_the_agent_and_settles_the_provisional_chat(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    _seed_creating_chat(agent_manager, ChatId("test-id"), "Chat 1")
    q = broadcaster.register()

    agent_manager._run_creation(ChatId("test-id"), "test-id", "test-agent", ["true"], tmp_path, {}, HarnessType.CLAUDE)

    assert agent_manager.get_provisional_chat("test-id") is None
    agent = agent_manager.get_agent_by_id("test-id")
    assert agent is not None
    assert agent.name == "test-agent"
    messages = []
    while not q.empty():
        raw = q.get_nowait()
        assert raw is not None
        messages.append(json.loads(raw))
    completed = [message for message in messages if message["type"] == "provisional_chat_completed"]
    assert completed == [{"type": "provisional_chat_completed", "chat_id": "test-id", "success": True, "error": None}]


def test_a_created_chat_stays_listed_through_observe_events_that_predate_it(
    agent_manager: AgentManager, tmp_path: Path
) -> None:
    """The observe stream reports a new agent seconds after its create returns; every event before
    that rebuilds the tracked agents without it, which must not unlist the chat the create landed."""
    created_id = str(MngrAgentId())
    _seed_creating_chat(agent_manager, ChatId(created_id), "Chat 1")
    agent_manager._run_creation(ChatId(created_id), created_id, "chat-1", ["true"], tmp_path, {}, HarnessType.CLAUDE)

    agent_manager._handle_observe_event(make_agent_state_event(_agent_details("older-chat")))
    agent_manager._handle_observe_event(make_full_agent_state_event([_agent_details("older-chat")]))

    assert created_id in {snapshot.chat_id for snapshot in agent_manager.get_chat_snapshots()}


def test_a_created_agent_the_observe_stream_never_reports_is_let_go(
    agent_manager: AgentManager, tmp_path: Path
) -> None:
    """A create that never reaches the stream (the agent died before observe saw it) must not be held
    for good: the full snapshots are how the stream says what exists, so two of them without it end it."""
    created_id = str(MngrAgentId())
    (tmp_path / "agents" / created_id).mkdir(parents=True)
    _seed_creating_chat(agent_manager, ChatId(created_id), "Chat 1")
    agent_manager._run_creation(ChatId(created_id), created_id, "chat-1", ["true"], tmp_path, {}, HarnessType.CLAUDE)
    with agent_manager._lock:
        assert created_id in agent_manager._activity_tracked_agents
        assert created_id in agent_manager._model_watcher_by_agent

    for _ in range(FULL_SNAPSHOTS_BEFORE_A_CREATED_AGENT_IS_LET_GO):
        agent_manager._handle_observe_event(make_full_agent_state_event([_agent_details("older-chat")]))

    assert agent_manager.get_agent_by_id(created_id) is None
    with agent_manager._lock:
        assert created_id not in agent_manager._activity_tracked_agents
        assert created_id not in agent_manager._model_watcher_by_agent


def test_the_observe_stream_owns_a_created_agent_once_it_reports_it(
    agent_manager: AgentManager, tmp_path: Path
) -> None:
    created_id = MngrAgentId()
    _seed_creating_chat(agent_manager, ChatId(str(created_id)), "Chat 1")
    agent_manager._run_creation(
        ChatId(str(created_id)), str(created_id), "chat-1", ["true"], tmp_path, {}, HarnessType.CLAUDE
    )
    created = _agent_details("chat-1", agent_id=created_id)

    agent_manager._handle_observe_event(make_agent_state_event(created))
    agent_manager._handle_observe_event(make_agent_removed_event(created.id, created.name, created.host.id))

    assert agent_manager.get_agent_by_id(str(created_id)) is None


def test_run_creation_tells_the_page_the_chat_landed_even_when_settling_it_fails(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """The agent is up, so the create succeeded; a first message that could not be handed over is the
    settling step's problem and must not cost the waiting page its answer."""
    _seed_creating_chat(agent_manager, ChatId("test-id"), "Chat 1")

    def deliver(agent_info: AgentInfo, text: str, message_id: str) -> SendOutcome:
        raise OSError("the pane went away")

    agent_manager.set_handoff_capabilities(
        HandoffCapabilities(
            ensure_watcher=lambda agent_info: ListTranscriptReader([]),
            drain_to_composer=lambda agent_info: "",
            deliver=deliver,
        )
    )
    q = broadcaster.register()

    with pytest.raises(OSError):
        agent_manager._run_creation(
            ChatId("test-id"),
            "test-id",
            "test-agent",
            ["true"],
            tmp_path,
            {},
            HarnessType.CLAUDE,
            deferred_message="hello",
        )

    assert agent_manager.get_agent_by_id("test-id") is not None
    messages = []
    while not q.empty():
        raw = q.get_nowait()
        assert raw is not None
        messages.append(json.loads(raw))
    completed = [message for message in messages if message["type"] == "provisional_chat_completed"]
    assert completed == [{"type": "provisional_chat_completed", "chat_id": "test-id", "success": True, "error": None}]


def test_run_creation_leaves_a_failed_chat_in_the_failed_phase_with_the_output_tail(
    agent_manager: AgentManager, tmp_path: Path
) -> None:
    """A failed create is not forgotten: the page shows why, and can try again on the same account."""
    _seed_creating_chat(agent_manager, ChatId("test-id"), "Chat 1")
    cmd = ["sh", "-c", "echo first line; echo the real reason >&2; exit 3"]

    agent_manager._run_creation(ChatId("test-id"), "test-id", "test-agent", cmd, tmp_path, {}, HarnessType.CLAUDE)

    assert agent_manager.get_agent_by_id("test-id") is None
    proto = agent_manager.get_provisional_chat("test-id")
    assert proto is not None
    assert proto.phase is ProvisionalChatPhase.FAILED
    assert proto.account_id == "acct-1"
    assert proto.error is not None
    assert proto.error.startswith("mngr create exited with code 3")
    assert "the real reason" in proto.error


def test_handle_observe_output_line_empty_is_ignored(agent_manager: AgentManager) -> None:
    """Empty lines from the observe subprocess are silently ignored."""
    agent_manager._handle_observe_output_line("   ", True)
    assert agent_manager.get_agents() == []


def test_handle_observe_output_line_raises_on_invalid_json(agent_manager: AgentManager) -> None:
    """Invalid JSON on stdout from mngr observe surfaces as JSONDecodeError so the upstream bug is visible."""
    with pytest.raises(json.JSONDecodeError):
        agent_manager._handle_observe_output_line("not json {", True)
    assert agent_manager.get_agents() == []


def test_handle_observe_output_line_dispatches_agent_state(
    agent_manager: AgentManager,
) -> None:
    """Valid AGENT_STATE JSONL lines are parsed and dispatched."""
    test_agent_id = MngrAgentId()
    agent = _agent_details("obs-agent", agent_id=test_agent_id)
    event = make_agent_state_event(agent)
    line = json.dumps(event.model_dump(mode="json"))

    agent_manager._handle_observe_output_line(line, True)

    agents = agent_manager.get_agents()
    assert len(agents) == 1
    assert agents[0].id == str(test_agent_id)


def test_handle_observe_event_dispatches_full_state(
    agent_manager: AgentManager,
) -> None:
    """AGENTS_FULL_STATE events surface every agent they carry."""
    test_agent_id = MngrAgentId()
    agent = _agent_details("snap-agent", agent_id=test_agent_id)
    event = make_full_agent_state_event([agent])
    agent_manager._handle_observe_event(event)

    agents = agent_manager.get_agents()
    assert len(agents) == 1
    assert agents[0].id == str(test_agent_id)


def test_handle_observe_event_dispatches_agent_state(
    agent_manager: AgentManager,
) -> None:
    """AGENT_STATE events upsert the single agent they carry."""
    test_agent_id = MngrAgentId()
    agent = _agent_details("disc-agent", agent_id=test_agent_id)
    event = make_agent_state_event(agent)
    agent_manager._handle_observe_event(event)

    agents = agent_manager.get_agents()
    assert len(agents) == 1
    assert agents[0].id == str(test_agent_id)


def test_handle_observe_event_dispatches_agent_removed(
    agent_manager: AgentManager,
) -> None:
    """AGENT_REMOVED events drop the referenced agent."""
    test_agent_id = MngrAgentId()
    agent = _agent_details("to-destroy", agent_id=test_agent_id)
    agent_manager._handle_observe_event(make_agent_state_event(agent))
    assert len(agent_manager.get_agents()) == 1

    agent_manager._handle_observe_event(make_agent_removed_event(agent.id, agent.name, agent.host.id))
    assert len(agent_manager.get_agents()) == 0


def test_full_snapshot_dropping_agents_removes_them(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster
) -> None:
    """A later full snapshot that omits previously-tracked agents drops them and broadcasts.

    There is no host event on the observe stream, so the way a whole host's worth
    of agents disappears is a rebuild snapshot that no longer lists them.
    """
    agent_id_1 = MngrAgentId()
    agent_id_2 = MngrAgentId()

    agents = [_agent_details(f"agent-{str(aid)[:8]}", agent_id=aid) for aid in (agent_id_1, agent_id_2)]
    agent_manager._handle_observe_event(make_full_agent_state_event(agents))
    assert len(agent_manager.get_agents()) == 2

    # Register after seeding so the queue captures only the drop broadcast.
    q = broadcaster.register()
    agent_manager._handle_observe_event(make_full_agent_state_event([]))

    assert len(agent_manager.get_agents()) == 0
    assert agent_manager.get_agent_matches_by_id(str(agent_id_1)) == []
    assert agent_manager.get_agent_matches_by_id(str(agent_id_2)) == []
    raw = q.get_nowait()
    assert raw is not None
    msg = json.loads(raw)
    assert msg["type"] == "chats_updated"


def test_full_snapshot_omitting_agent_drops_it(
    agent_manager: AgentManager,
) -> None:
    """A rebuild snapshot that no longer lists a tracked agent drops it from the set."""
    agent_id = MngrAgentId()
    agent = _agent_details("host-agent", agent_id=agent_id)
    agent_manager._handle_observe_event(make_full_agent_state_event([agent]))
    assert len(agent_manager.get_agents()) == 1

    agent_manager._handle_observe_event(make_full_agent_state_event([]))
    assert len(agent_manager.get_agents()) == 0


def test_build_observe_command_honors_injected_binary(broadcaster: WebSocketBroadcaster) -> None:
    """The ``mngr_binary`` argument to ``build()`` overrides the default binary path."""
    manager = AgentManager.build(broadcaster, mngr_binary="/path/to/custom-mngr")
    try:
        cmd = manager._build_observe_command()
        assert cmd == ["/path/to/custom-mngr", "observe", "--stream-events"]
    finally:
        manager.stop()


# --- mngr CLI argv contract ---
# These confront each builder's argv with the live ``imbue.mngr.main.cli`` tree,
# so a system/vendor/mngr subcommand/flag rename fails here at merge time rather than
# only surfacing at runtime. See ``mngr_cli_contract`` for the validator.


def _chat_create_argv(**overrides: Any) -> list[str]:
    """The argv for one demo chat on claude; ``overrides`` replace the builder's arguments."""
    arguments: dict[str, Any] = {
        "mngr_binary": "mngr",
        "name": "demo",
        "chat_id": ChatId("agent-123"),
        "agent_id": "agent-123",
        "primary_labels": {},
        "harness": HarnessType.CLAUDE,
    }
    return _build_chat_create_command(**{**arguments, **overrides})


def test_chat_create_argv_selects_harness_by_type_and_role_by_template() -> None:
    """The harness/role split is the contract: `--type` picks the harness, the lone
    `--template` picks the role.

    The harness rides `--type <harness>` (resolving `[agent_types.<harness>]`
    directly), and the `chat` role template -- which never sets `type` -- cannot
    clobber it.
    """
    argv = _chat_create_argv()
    assert argv[argv.index("--type") + 1] == HarnessType.CLAUDE
    templates = [argv[i + 1] for i, tok in enumerate(argv) if tok == "--template"]
    assert templates == ["chat"]


def test_chat_create_argv_names_the_chat_in_the_agents_environment() -> None:
    """Every agent the app creates carries its chat's id as ``MINDS_CHAT_ID``, which is how a
    skill or script inside the workspace addresses the chat rather than the agent."""
    argv = _chat_create_argv()
    env_values = [argv[i + 1] for i, tok in enumerate(argv) if tok == "--env"]
    assert "MINDS_CHAT_ID=agent-123" in env_values


def test_codex_chat_create_argv_accepted_by_live_cli() -> None:
    """The codex harness reuses the chat role verbatim; only the `--type` differs."""
    argv = _chat_create_argv(
        primary_labels={"project": "proj"},
        harness=HarnessType.CODEX,
    )
    assert_mngr_argv_valid(argv)
    assert argv[argv.index("--type") + 1] == HarnessType.CODEX
    templates = [argv[i + 1] for i, tok in enumerate(argv) if tok == "--template"]
    assert templates == ["chat"]


def test_chat_create_argv_carries_a_seeded_first_message_only_when_given() -> None:
    """The seeded message rides the create as ``--message`` (delivered once the harness is ready,
    like ``/welcome``); a plain chat's argv carries no ``--message`` at all."""
    seeded = _chat_create_argv(initial_message="/use-template https://github.com/example/a-template")
    assert_mngr_argv_valid(seeded)
    assert seeded[seeded.index("--message") + 1] == "/use-template https://github.com/example/a-template"

    plain = _chat_create_argv()
    assert "--message" not in plain


def test_chat_create_argv_accepted_by_live_cli() -> None:
    argv = _chat_create_argv(primary_labels={"workspace": "ws", "project": "proj"})
    assert_mngr_argv_valid(argv)
    # The chat carries user_created so the OOM launch wrapper puts it in the
    # dynamic chat band rather than the least-protected worker/unclassified band.
    assert "user_created=true" in argv


def test_every_harness_launches_through_the_oom_band_wrapper() -> None:
    """Each harness's ``[agent_types.<harness>]`` sends its launch through the OOM band
    wrapper, naming its own binary.

    A harness with no ``command`` runs unbanded: earlyoom then sheds it by raw kernel
    score instead of the user/worker tiering, so it can take a user's chat before a
    worker's build subprocess. That is not loud -- nothing fails, the agent just becomes
    disproportionately likely to be killed -- so it is pinned here rather than left to be
    noticed. Driven off ``HarnessType`` so a newly registered harness fails this until it
    is wired up, which is exactly how codex and pi went unbanded.
    """
    settings = tomllib.loads((Path(__file__).parents[5] / ".mngr" / "settings.toml").read_text())
    agent_types = settings["agent_types"]
    for harness in HarnessType:
        if harness is HarnessType.SEED:
            # The seed segment's pseudo-harness: no agent ever runs on it, so it has no agent
            # type and nothing to launch through the wrapper.
            continue
        command = agent_types[harness.value].get("command", "")
        assert "oom_priority/bin/agent_oom_launch.py" in command, f"{harness} launches unbanded"
        # The wrapper consumes argv[1] as the binary to exec, so it must actually be there.
        assert command.split()[-1], f"{harness} names the wrapper with no binary to exec"


def test_chat_create_argv_carries_no_launch_settings() -> None:
    """Plain chats launch at the harness defaults: no `-S` overrides at all. Fast
    mode rides only the `fast` create template (see .mngr/settings.toml), never
    the argv, so a chat launched without it starts at standard speed."""
    argv = _chat_create_argv()
    assert "-S" not in argv
    assert not any("fastMode" in token for token in argv)


def test_chat_create_argv_stacks_extra_role_templates_after_chat() -> None:
    """The launch templates (`welcome`, `fast`) stack via extra_role_templates; the
    resulting argv must resolve against the live CLI."""
    argv = _chat_create_argv(
        harness=HarnessType.CODEX,
        extra_role_templates=("welcome", "fast"),
    )
    assert_mngr_argv_valid(argv)
    templates = [argv[i + 1] for i, tok in enumerate(argv) if tok == "--template"]
    assert templates == ["chat", "welcome", "fast"]


# --- the chat's originating project (the mngr ``project`` label) ---
# A chat is an agent, so the project it was created inside rides the label mngr
# already propagates to the agent's children rather than a parallel list. The
# label is where a chat starts out filed, not an owner: membership is
# many-to-many and each view's member list says what that view shows.


def test_chat_project_label_prefers_the_project_the_chat_was_created_in() -> None:
    assert _chat_project_label({"project": "taxes"}, "website-redesign") == "website-redesign"


def test_chat_project_label_inherits_the_primary_agents_project_outside_any_project() -> None:
    assert _chat_project_label({"project": "taxes"}, "") == "taxes"


def test_chat_project_label_is_empty_when_nothing_names_a_project() -> None:
    """A chat filed in no project is fine -- Everything lists every object anyway."""
    assert _chat_project_label({}, "") == ""


def test_chat_create_argv_canonicalizes_the_name_and_labels_the_human_one() -> None:
    """A chat is created under its true name with the typed name as a label.

    Both are sent explicitly so the create works against any vendored mngr,
    including one predating free-form names -- and the pair is what newer mngr
    derives for itself, so its "true name is the canonical form of the display
    name" rule holds either way.
    """
    argv = _chat_create_argv(name="Chat 2", chat_id=ChatId("agent-1"), agent_id="agent-1")

    assert argv[2] == "Chat-2"
    labels = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--label"]
    assert "display_name=Chat 2" in labels
    assert_mngr_argv_valid(argv)


def test_a_successor_create_argv_is_accepted_by_the_live_cli() -> None:
    """A handoff's create adds the chat membership labels after the account args, which the vendored mngr has
    to accept.

    That the successor's create carries no message -- the prompt follows through the send path once the
    model pick has landed -- is checked where the create is actually built, against the fake mngr's own
    argv log (``chat_handoffs_test.py``); asserting it here would only restate that this helper passes
    no message.
    """
    argv = _chat_create_argv(
        account_args=("--label", "account=acct-1"),
        extra_labels=("chat_id=agent-123", "chat_seq=2"),
    )
    assert_mngr_argv_valid(argv)
    labels = [argv[i + 1] for i, token in enumerate(argv) if token == "--label"]
    assert labels[-3:] == ["account=acct-1", "chat_id=agent-123", "chat_seq=2"]


def test_chat_rename_argv_accepted_by_live_cli() -> None:
    """A rename carries the same name pair a create does: canonical name + typed label.

    The canonical name is what an older vendored mngr accepts, and the typed
    name rides the same atomic write as the rename so no observer sees the
    renamed agent without its ``display_name``.
    """
    argv = _build_chat_rename_command(mngr_binary="mngr", agent_id="agent-123", name="Planning notes")
    assert_mngr_argv_valid(argv)
    assert argv == ["mngr", "rename", "agent-123", "Planning-notes", "--label", "display_name=Planning notes"]


def test_chat_display_label_argv_accepted_by_live_cli() -> None:
    """A display-only rename rewrites the label without renaming anything."""
    argv = _build_chat_display_label_command(mngr_binary="mngr", agent_id="agent-123", name="Chat 2")
    assert_mngr_argv_valid(argv)
    assert argv == ["mngr", "label", "agent-123", "--label", "display_name=Chat 2"]


def _tracked_chat(manager: AgentManager, agent_id: str, name: str, display_name: str | None = None) -> None:
    labels = {} if display_name is None else {"display_name": display_name}
    with manager._lock:
        manager._agents[agent_id] = AgentStateItem(
            id=agent_id, name=name, state="RUNNING", labels=labels, work_dir=None
        )


def test_rename_chat_refuses_a_chat_that_is_still_being_created(
    broadcaster: WebSocketBroadcaster,
) -> None:
    """A create in flight already carries a name; renaming to another would race it.

    Filing the name the chat is *already* being created under is the ordinary
    case and is a no-op here; anything else is refused rather than silently
    diverging from whatever the create ends up writing.
    """
    manager = AgentManager.build(broadcaster)
    try:
        with manager._lock:
            manager._provisional_chats[ChatId("proto-1")] = ProvisionalChat(
                chat_id=ChatId("proto-1"), name="Chat 2", phase=ProvisionalChatPhase.CREATING
            )
        manager.rename_chat("proto-1", "Chat 2")
        with pytest.raises(AgentRenameError):
            manager.rename_chat("proto-1", "Something else")
    finally:
        manager.stop()


def test_rename_chat_leaves_mngr_alone_for_an_untracked_id(
    broadcaster: WebSocketBroadcaster,
    false_binary: str,
) -> None:
    """An id belonging to no agent has no mngr name to diverge from.

    The stand-in binary always exits non-zero, so this returning quietly is the
    proof that nothing was run: an actual invocation would have raised.
    """
    manager = AgentManager.build(broadcaster, mngr_binary=false_binary)
    try:
        manager.rename_chat("agent-nowhere", "Scratch")
    finally:
        manager.stop()


def test_rename_chat_raises_when_mngr_refuses(
    broadcaster: WebSocketBroadcaster,
    false_binary: str,
) -> None:
    """A non-zero ``mngr rename`` is an error, and the agent keeps its old name.

    The caller (the member-title endpoint) turns this into an error response and
    records nothing, so the two names cannot drift apart unnoticed.
    """
    manager = AgentManager.build(broadcaster, mngr_binary=false_binary)
    try:
        _tracked_chat(manager, "agent-7", "Chat-2")
        with pytest.raises(AgentRenameError):
            manager.rename_chat("agent-7", "Planning notes")
        still_named = manager.get_agent_by_id("agent-7")
        assert still_named is not None
        assert still_named.name == "Chat-2"
    finally:
        manager.stop()


def test_rename_chat_rejects_a_name_already_held_by_another_chat(
    broadcaster: WebSocketBroadcaster,
    false_binary: str,
) -> None:
    """Names collide by canonical form, before mngr is even asked.

    "chat 3" canonicalizes to another agent's true name, so the rename is
    refused with the conflict error the endpoint answers 409 with -- and the
    always-failing stand-in binary proves the check fired first.
    """
    manager = AgentManager.build(broadcaster, mngr_binary=false_binary)
    try:
        _tracked_chat(manager, "agent-7", "Chat-2", display_name="Chat 2")
        _tracked_chat(manager, "agent-8", "Chat-3", display_name="Chat 3")
        with pytest.raises(AgentNameConflictError):
            manager.rename_chat("agent-7", "chat 3")
    finally:
        manager.stop()


def test_rename_chat_refuses_the_primary_agent(
    broadcaster: WebSocketBroadcaster,
    false_binary: str,
) -> None:
    """The services agent's name belongs to the minds app, not to a chat tab."""
    manager = AgentManager.build(broadcaster, mngr_binary=false_binary)
    try:
        with manager._lock:
            manager._agents["agent-1"] = AgentStateItem(
                id="agent-1", name="system-services", state="RUNNING", labels={"is_primary": "true"}, work_dir=None
            )
        with pytest.raises(AgentRenameError):
            manager.rename_chat("agent-1", "My machine")
    finally:
        manager.stop()


def test_rename_chat_rejects_a_name_with_no_usable_characters(
    broadcaster: WebSocketBroadcaster,
    false_binary: str,
) -> None:
    manager = AgentManager.build(broadcaster, mngr_binary=false_binary)
    try:
        _tracked_chat(manager, "agent-7", "Chat-2")
        with pytest.raises(AgentRenameError):
            manager.rename_chat("agent-7", "!!!")
    finally:
        manager.stop()


def _finished_rename(returncode: int, stderr: str, is_timed_out: bool = False) -> FinishedProcess:
    """A rename subprocess's result, as ``run_local_command_modern_version`` shapes it."""
    return FinishedProcess(
        returncode=returncode,
        stdout="",
        stderr=stderr,
        command=("mngr", "rename"),
        is_timed_out=is_timed_out,
        is_output_already_logged=False,
    )


def test_rename_failure_detail_names_our_own_timeout_rather_than_a_signal_number() -> None:
    """A timed-out rename is our cap, and has to read like one.

    ``run_local_command_modern_version`` SIGTERMs a run that hits its timeout
    and reports the kill as a negative return code, so the old wording turned
    the cap in ``_RENAME_TIMEOUT_SECONDS`` into "rename exited with code -15"
    -- which tells the user neither what went wrong nor whether the name landed.
    """
    detail = _rename_failure_detail(
        ["mngr", "rename", "agent-1", "Docs"], _finished_rename(-signal.SIGTERM, "", is_timed_out=True)
    )
    assert "did not finish within" in detail
    assert "-15" not in detail
    # It must not claim the rename did not happen: the subprocess was stopped
    # partway through work that spans the provider's data and a live tmux session.
    assert "may or may not have been applied" in detail


def test_rename_failure_detail_still_reads_as_a_timeout_when_the_kill_had_to_escalate() -> None:
    # A SIGTERM the process ignored escalates to SIGKILL, but the cause is
    # still the timeout -- which is why the wording keys off ``is_timed_out``
    # rather than off which signal did the stopping.
    detail = _rename_failure_detail(["mngr", "rename"], _finished_rename(-signal.SIGKILL, "", is_timed_out=True))
    assert "did not finish within" in detail


def test_rename_failure_detail_prefers_what_the_command_actually_said() -> None:
    detail = _rename_failure_detail(["mngr", "rename"], _finished_rename(1, "  name already taken  "))
    assert detail == "name already taken"


def test_rename_failure_detail_reports_an_ordinary_exit_code_as_one() -> None:
    detail = _rename_failure_detail(["mngr", "rename"], _finished_rename(2, ""))
    assert detail == "'rename' exited with code 2"


def test_rename_failure_detail_names_a_signal_we_did_not_send() -> None:
    # No ``is_timed_out``, so this SIGTERM was not our cap (the OOM shedder,
    # say) and must not be dressed up as one.
    assert _rename_failure_detail(["mngr", "rename"], _finished_rename(-signal.SIGTERM, "")) == (
        "'rename' was stopped by signal 15"
    )
    assert _rename_failure_detail(["mngr", "rename"], _finished_rename(-signal.SIGKILL, "")) == (
        "'rename' was stopped by signal 9"
    )


def test_create_chat_mints_the_first_free_numbered_name(
    agent_manager: AgentManager,
) -> None:
    """An empty requested name allocates "Chat N" server-side, filling gaps.

    "Chat 1" and "Chat 3" are held by live agents' display labels, so the mint
    lands on "Chat 2" -- and its canonical form is the agent's name.
    """
    _tracked_chat(agent_manager, "agent-1", "Chat-1", display_name="Chat 1")
    _tracked_chat(agent_manager, "agent-3", "Chat-3", display_name="Chat 3")
    created = agent_manager.create_chat("")
    agent_manager.stop()

    assert created.display_name == "Chat 2"
    assert created.name == "Chat-2"


def test_create_chat_counts_in_flight_creates_as_taken(
    agent_manager: AgentManager,
) -> None:
    """Two concurrent creates cannot both mint "Chat 1": an in-flight create's
    proto entry blocks the slot. The in-flight create is pinned as a proto
    entry directly, so the test cannot race its background completion."""
    with agent_manager._lock:
        agent_manager._provisional_chats[ChatId("proto-1")] = ProvisionalChat(
            chat_id=ChatId("proto-1"), name="Chat 1", phase=ProvisionalChatPhase.CREATING
        )
    created = agent_manager.create_chat("")
    agent_manager.stop()

    assert created.display_name == "Chat 2"


def test_create_chat_numbers_every_harness_under_the_one_chat_word(
    agent_manager: AgentManager,
    tmp_path: Path,
) -> None:
    """A codex chat is "Chat 2", not "Codex 1": one word for every harness and lane.

    A chat can move to another harness after it is named, so a name that said which
    harness it started on would be wrong the moment it moved; the provider row of the
    model bar is where the harness shows.
    """
    # The plain chat is created first, while there is nothing signed in, so it lands on
    # the workspace login as claude. Signing in afterwards is what makes the second one
    # codex -- the harness comes from the bound account, never from the name.
    chat = agent_manager.create_chat("")
    codex_account_id, _ = mint_account_dir()
    commit_account(codex_account_id, "openai", "OpenAI")
    codex = agent_manager.create_chat("", account_id=codex_account_id)
    agent_manager.stop()

    assert chat.display_name == "Chat 1"
    assert codex.display_name == "Chat 2"


def test_create_chat_rejects_an_explicit_name_that_collides(
    agent_manager: AgentManager,
) -> None:
    """An explicitly requested name that canonicalizes onto an existing agent's
    true name is refused up front (the endpoint answers 409), not left for the
    background mngr create to fail on."""
    _tracked_chat(agent_manager, "agent-1", "Chat-2", display_name="Chat 2")
    with pytest.raises(AgentNameConflictError):
        agent_manager.create_chat("chat 2")
    agent_manager.stop()


def test_create_chat_registers_the_pre_observe_state_under_the_name_pair(
    broadcaster: WebSocketBroadcaster,
    git_work_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    true_binary: str,
) -> None:
    """The AgentStateItem registered before observe relists carries the same
    canonical name + display_name label the created mngr agent will hold, so
    the UI renders identically before and after the relist.

    ``true`` stands in for a succeeding ``mngr create``; the agent's registration is the
    "creation thread finished" signal.
    """
    monkeypatch.setenv("MNGR_AGENT_ID", "test-agent-id")
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(git_work_dir))
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    manager = AgentManager.build(broadcaster, mngr_binary=true_binary, chat_files_root=tmp_path / "chats")
    try:
        created = manager.create_chat("My planning chat")
        assert created.name == "My-planning-chat"

        wait_for(
            lambda: manager.get_agent_by_id(created.chat_id) is not None,
            timeout=30.0,
            error_message="the creation thread never registered the agent",
        )

        agent = manager.get_agent_by_id(created.chat_id)
        assert agent is not None
        assert agent.name == "My-planning-chat"
        assert agent.labels["display_name"] == "My planning chat"
        assert agent.labels["user_created"] == "true"
    finally:
        manager.stop()


def test_chat_create_argv_labels_the_project_the_chat_was_created_in() -> None:
    argv = _chat_create_argv(
        primary_labels={"workspace": "ws", "project": "taxes"},
        project_id="website-redesign",
    )
    assert "project=website-redesign" in argv
    assert "project=taxes" not in argv
    assert_mngr_argv_valid(argv)


def test_chat_create_argv_omits_the_project_label_when_there_is_no_project() -> None:
    argv = _chat_create_argv(primary_labels={"workspace": "ws"})
    assert not any(token.startswith("project=") for token in argv)
    assert_mngr_argv_valid(argv)


def test_serialized_agents_expose_the_project_label(broadcaster: WebSocketBroadcaster) -> None:
    """The workspace reads each chat's originating project off the agent payload."""
    manager = AgentManager.build(broadcaster)
    try:
        with manager._lock:
            for agent_id, labels in (
                ("filed", {"user_created": "true", "project": "taxes"}),
                ("unfiled", {"user_created": "true"}),
            ):
                manager._agents[agent_id] = AgentStateItem(
                    id=agent_id, name=agent_id, state="RUNNING", labels=labels, work_dir=None
                )
        project_by_id = {snapshot.chat_id: snapshot.project for snapshot in manager.get_chat_snapshots()}
        assert project_by_id == {"filed": "taxes", "unfiled": None}
    finally:
        manager.stop()


def test_serialized_agents_expose_the_display_name_label(broadcaster: WebSocketBroadcaster) -> None:
    """The human-readable name mngr holds rides along; ``name`` stays the true mngr name."""
    manager = AgentManager.build(broadcaster)
    try:
        with manager._lock:
            for agent_id, name, labels in (
                ("named", "Chat-1", {"user_created": "true", "display_name": "Chat 1"}),
                ("unnamed", "brave-otter", {"user_created": "true"}),
            ):
                manager._agents[agent_id] = AgentStateItem(
                    id=agent_id, name=name, state="RUNNING", labels=labels, work_dir=None
                )
        by_id = {snapshot.chat_id: snapshot for snapshot in manager.get_chat_snapshots()}
        assert by_id[ChatId("named")].title == "Chat 1"
        assert by_id[ChatId("unnamed")].title == "brave-otter"
        assert by_id[ChatId("named")].name == "Chat-1"
        assert by_id[ChatId("unnamed")].name == "brave-otter"
    finally:
        manager.stop()


def test_get_chat_ids_excludes_workers_and_primary(broadcaster: WebSocketBroadcaster) -> None:
    """Only chats are OOM-managed: workers and the primary keep their launch bands."""
    manager = AgentManager.build(broadcaster)
    try:
        with manager._lock:
            for agent_id, labels in (
                ("chat", {"user_created": "true"}),
                ("worker", {"agent_created": "true"}),
                ("primary", {"is_primary": "true"}),
            ):
                manager._agents[agent_id] = AgentStateItem(
                    id=agent_id, name=agent_id, state="RUNNING", labels=labels, work_dir=None
                )
        assert manager.get_chat_ids() == ["chat"]
    finally:
        manager.stop()


def test_get_running_chat_agent_names_excludes_dead_workers_and_primary(broadcaster: WebSocketBroadcaster) -> None:
    """Only running chats are autocompacted: workers, primary, and dead chats are excluded."""
    manager = AgentManager.build(broadcaster)
    try:
        with manager._lock:
            for agent_id, state, labels in (
                ("chat-running", "RUNNING", {"user_created": "true"}),
                ("chat-waiting", "WAITING", {"user_created": "true"}),
                ("chat-dead", "DEAD", {"user_created": "true"}),
                ("chat-stopped", "STOPPED", {"user_created": "true"}),
                ("worker-running", "RUNNING", {"agent_created": "true"}),
                ("primary-running", "RUNNING", {"is_primary": "true"}),
            ):
                manager._agents[agent_id] = AgentStateItem(
                    id=agent_id, name=f"{agent_id}-name", state=state, labels=labels, work_dir=None
                )
        assert manager.get_running_chat_agent_names() == ["chat-running-name", "chat-waiting-name"]
    finally:
        manager.stop()


def test_agent_manager_autocompactor_custom_injection_and_lifecycle(
    broadcaster: WebSocketBroadcaster,
) -> None:
    custom_compactor = ChatAutoCompactor.build(
        list_running_chat_agent_names=lambda: [],
        runner=lambda *args, **kwargs: FinishedProcess(
            command=(), returncode=0, stdout="", stderr="", is_timed_out=False, is_output_already_logged=False
        ),
    )
    manager = AgentManager.build(broadcaster, autocompactor=custom_compactor)
    assert manager._autocompactor is custom_compactor
    assert manager._autocompactor._thread is None

    manager._autocompactor.start()
    assert manager._autocompactor._thread is not None
    assert manager._autocompactor._thread.is_alive()

    manager.stop()
    assert manager._autocompactor._thread is None


def test_observe_argv_accepted_by_live_cli() -> None:
    argv = _build_observe_command_argv("mngr")
    assert_mngr_argv_valid(argv)
    assert "--stream-events" in argv


def test_resolve_observe_cwd_prefers_existing_work_dir(
    broadcaster: WebSocketBroadcaster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``MNGR_AGENT_WORK_DIR`` points at a real directory, observe runs there."""
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(tmp_path))
    manager = AgentManager.build(broadcaster)
    try:
        assert manager._resolve_observe_cwd() == tmp_path
    finally:
        manager.stop()


def test_resolve_observe_cwd_falls_back_when_work_dir_missing(
    broadcaster: WebSocketBroadcaster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``MNGR_AGENT_WORK_DIR`` is set but the path does not exist, use ``$HOME``.

    Guards the fallback that keeps observe runnable in tests that stub the env
    var with a non-existent path (e.g. the shared ``agent_manager`` fixture).
    """
    missing = tmp_path / "does-not-exist"
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(missing))
    manager = AgentManager.build(broadcaster)
    try:
        assert manager._resolve_observe_cwd() == Path.home()
    finally:
        manager.stop()


def test_resolve_observe_cwd_falls_back_when_work_dir_unset(
    broadcaster: WebSocketBroadcaster,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``MNGR_AGENT_WORK_DIR`` unset, observe runs from ``$HOME``."""
    monkeypatch.delenv("MNGR_AGENT_WORK_DIR", raising=False)
    manager = AgentManager.build(broadcaster)
    try:
        assert manager._resolve_observe_cwd() == Path.home()
    finally:
        manager.stop()


def test_start_observe_spawns_long_lived_subprocess(
    broadcaster: WebSocketBroadcaster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: the observe subprocess stays alive after startup.

    A healthy ``mngr observe`` keeps running until it is explicitly stopped;
    this test asserts that after ``_start_observe`` returns, the child is
    still running a short window later rather than having exited on its own.
    """
    if shutil.which("mngr") is None:
        pytest.skip("mngr binary not on PATH")

    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))
    # Point the subprocess at a clean cwd with no project-local .mngr/settings.toml;
    # otherwise running pytest from inside a mngr-managed worktree would inherit
    # a config with ``is_allowed_in_pytest = false`` and the child would abort.
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(tmp_path))
    # And at an empty project config dir: the account this module's autouse fixture commits
    # writes a settings.local.toml into the shared one, which carries no
    # ``is_allowed_in_pytest`` and so would make the child abort the same way.
    monkeypatch.setenv("MNGR_PROJECT_CONFIG_DIR", str(tmp_path / "mngr-project-config"))
    # And at an empty host dir: with the developer's real ~/.mngr, the spawned
    # observe enumerates their live agents and queries tmux about them, which
    # trips the tmux resource guard on any machine with running agents. The
    # test only asserts the child stays alive, which an empty world satisfies.
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path / "mngr-host"))
    manager = AgentManager.build(broadcaster)
    try:
        manager._start_observe()
        assert manager._observe_process is not None
        # If the subprocess exits within the window it's a failure (bad command,
        # crashed on startup, etc.). A healthy observe keeps running.
        exited = poll_until(
            lambda: manager._observe_process is not None and manager._observe_process.poll() is not None,
            timeout=1.5,
            poll_interval=0.1,
        )
        assert not exited, (
            "mngr observe subprocess exited within 1.5s of startup "
            f"(returncode={manager._observe_process.returncode}); stderr: "
            f"{manager._observe_process.read_stderr()!r}"
        )
    finally:
        manager.stop()


def test_start_observe_logs_error_when_subprocess_exits_unexpectedly(
    broadcaster: WebSocketBroadcaster,
    false_binary: str,
    loguru_records: list[str],
) -> None:
    """If the observe subprocess exits on its own, the watchdog logs an ERROR.

    Uses ``/usr/bin/false`` (or equivalent) as a stand-in mngr binary so the
    spawned process exits immediately with a non-zero code.
    """
    manager = AgentManager.build(broadcaster, mngr_binary=false_binary)
    try:
        manager._start_observe()
        logged_error = poll_until(
            lambda: any(r.startswith("ERROR") and "mngr observe" in r for r in loguru_records),
            timeout=5.0,
            poll_interval=0.05,
        )
        assert logged_error, (
            "Expected an ERROR log from the observe watchdog; got: "
            f"{[r for r in loguru_records if r.startswith('ERROR')]}"
        )
    finally:
        manager.stop()


def test_start_observe_watchdog_stays_quiet_on_clean_shutdown(
    broadcaster: WebSocketBroadcaster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[str],
) -> None:
    """Calling ``stop()`` on a healthy observe subprocess must not produce errors."""
    if shutil.which("mngr") is None:
        pytest.skip("mngr binary not on PATH")

    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))
    # See test_start_observe_spawns_long_lived_subprocess for why these are needed.
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path / "mngr-host"))
    manager = AgentManager.build(broadcaster)
    manager._start_observe()
    # ``_start_observe`` only returns after ``run_process_in_background``
    # has spawned the child and its RunningProcess thread has started, so the
    # subprocess is guaranteed to be running by the time we call stop().
    assert manager._observe_process is not None
    manager.stop()

    errors = [r for r in loguru_records if r.startswith("ERROR") and "mngr observe" in r]
    assert errors == [], f"Watchdog logged errors during clean shutdown: {errors}"


def test_handle_observe_output_line_logs_stderr_as_warning(
    agent_manager: AgentManager,
    loguru_records: list[str],
) -> None:
    """Stderr output from the observe subprocess is surfaced as a warning."""
    agent_manager._handle_observe_output_line("something bad happened", is_stdout=False)

    warnings = [r for r in loguru_records if r.startswith("WARNING") and "mngr observe stderr" in r]
    assert warnings, f"Expected a stderr warning; got: {loguru_records}"
    assert "something bad happened" in warnings[0]


# ---------------------------------------------------------------------------
# Activity-state integration
# ---------------------------------------------------------------------------


def test_ensure_activity_tracking_skips_when_state_dir_missing(agent_manager: AgentManager) -> None:
    """No activity tracking is started for an agent whose host_dir state directory is absent."""
    _seed_agent(agent_manager, "remote-agent")
    agent_manager._ensure_activity_tracking("remote-agent")
    try:
        with agent_manager._lock:
            assert "remote-agent" not in agent_manager._activity_tracked_agents
    finally:
        agent_manager.stop()


def test_ensure_activity_tracking_seeds_idle_state_silently(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """When the state dir exists, the agent is seeded as IDLE without broadcasting."""
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")

    listener = broadcaster.register()
    try:
        agent_manager._ensure_activity_tracking("agent-1")
        # No broadcast should have happened (lifecycle handlers broadcast separately).
        with pytest.raises(queue.Empty):
            listener.get_nowait()

        with agent_manager._lock:
            assert agent_manager._activity_state_by_agent["agent-1"] == ActivityState.IDLE
            assert agent_manager._agents["agent-1"].activity_state == ActivityState.IDLE.value
    finally:
        agent_manager.stop()


def test_waiting_lifecycle_trusts_the_active_marker(agent_manager: AgentManager, tmp_path: Path) -> None:
    """The recompute's own wiring: the tracker-declared turn marker is statted and fed to
    derive, so a WAITING agent with a live `active` marker reads THINKING (the observe
    stream can miss a short turn; the marker flips promptly) and settles once it clears."""
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1", state="WAITING")
    agent_manager._ensure_activity_tracking("agent-1")
    agent_manager.update_session_events(
        "agent-1", [{"type": "user_message", "timestamp": "2026-07-28T00:00:00Z", "content": "go"}]
    )
    assert agent_manager._activity_state_by_agent.get("agent-1") == ActivityState.IDLE

    (state_dir / "active").touch()
    agent_manager._recompute_activity_state("agent-1", broadcast_on_change=False)
    assert agent_manager._activity_state_by_agent.get("agent-1") == ActivityState.THINKING

    (state_dir / "active").unlink()
    agent_manager._recompute_activity_state("agent-1", broadcast_on_change=False)
    assert agent_manager._activity_state_by_agent.get("agent-1") == ActivityState.IDLE


def test_session_events_user_message_drives_thinking(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """A user_message at the tail of the transcript flips activity_state to THINKING.

    Replaces the old behavior where THINKING was driven by a transient ``active``
    marker file -- that marker could leak past the end of a turn and falsely
    pin the indicator on "Thinking...". Transcript content is now authoritative.
    """
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    agent_manager._ensure_activity_tracking("agent-1")

    listener = broadcaster.register()
    try:
        agent_manager.update_session_events(
            "agent-1",
            [{"type": "user_message", "content": "go"}],
        )
        with agent_manager._lock:
            assert agent_manager._activity_state_by_agent["agent-1"] == ActivityState.THINKING
        latest = _last_chats_updated(_drain(listener))
        assert latest is not None
        agents = [chat["active_agent"] for chat in latest["chats"]]
        assert isinstance(agents, list)
        assert agents[0]["activity_state"] == ActivityState.THINKING.value
    finally:
        agent_manager.stop()


def test_session_events_assistant_message_at_tail_is_idle(agent_manager: AgentManager, tmp_path: Path) -> None:
    """An assistant_message with no pending tools at the tail means IDLE."""
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    agent_manager._ensure_activity_tracking("agent-1")

    try:
        agent_manager.update_session_events(
            "agent-1",
            [
                {"type": "user_message", "content": "go"},
                {"type": "assistant_message", "tool_calls": []},
            ],
        )
        with agent_manager._lock:
            assert agent_manager._activity_state_by_agent["agent-1"] == ActivityState.IDLE
    finally:
        agent_manager.stop()


def test_update_session_events_flips_to_tool_running(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    agent_manager._ensure_activity_tracking("agent-1")

    listener = broadcaster.register()
    try:
        events_with_pending: list[dict[str, Any]] = [
            {
                "type": "assistant_message",
                "tool_calls": [{"tool_call_id": "call_a", "tool_name": "Bash"}],
            }
        ]
        agent_manager.update_session_events("agent-1", events_with_pending)

        with agent_manager._lock:
            assert agent_manager._activity_state_by_agent["agent-1"] == ActivityState.TOOL_RUNNING

        latest = _last_chats_updated(_drain(listener))
        assert latest is not None
        agents = [chat["active_agent"] for chat in latest["chats"]]
        assert isinstance(agents, list)
        assert agents[0]["activity_state"] == ActivityState.TOOL_RUNNING.value

        # Once the result lands, we flip to THINKING (last event is tool_result,
        # no pending tool_use remains).
        events_resolved = events_with_pending + [{"type": "tool_result", "tool_call_id": "call_a"}]
        agent_manager.update_session_events("agent-1", events_resolved)
        with agent_manager._lock:
            assert agent_manager._activity_state_by_agent["agent-1"] == ActivityState.THINKING
    finally:
        agent_manager.stop()


def test_update_session_events_no_op_when_not_tracked(agent_manager: AgentManager) -> None:
    """Calling update_session_events for an untracked agent is a quiet no-op.

    Beyond not raising, it must leave no residue in the per-agent caches:
    otherwise those entries would never be cleared (``_stop_activity_tracking``
    only fires for agents that were being tracked), accumulating indefinitely.
    """
    agent_manager.update_session_events(
        "ghost",
        [{"type": "assistant_message", "tool_calls": [{"tool_call_id": "x", "tool_name": "Bash"}]}],
    )
    with agent_manager._lock:
        assert "ghost" not in agent_manager._activity_state_by_agent
        assert "ghost" not in agent_manager._activity_tracker_by_agent


def test_reset_activity_state_clears_tool_running(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """reset_activity_state flips a stuck TOOL_RUNNING agent back to IDLE and broadcasts.

    Models the interrupt flow: the agent has an unmatched tool_use in its
    transcript (TOOL_RUNNING), then gets restarted. The restart leaves the
    transcript mid-turn, so without an explicit reset the indicator would
    stay pinned at TOOL_RUNNING.
    """
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    agent_manager._ensure_activity_tracking("agent-1")

    listener = broadcaster.register()
    try:
        agent_manager.update_session_events(
            "agent-1",
            [{"type": "assistant_message", "tool_calls": [{"tool_call_id": "call_a", "tool_name": "Bash"}]}],
        )
        with agent_manager._lock:
            assert agent_manager._activity_state_by_agent["agent-1"] == ActivityState.TOOL_RUNNING

        agent_manager.reset_activity_state("agent-1")

        with agent_manager._lock:
            assert agent_manager._activity_state_by_agent["agent-1"] == ActivityState.IDLE
            assert agent_manager._agents["agent-1"].activity_state == ActivityState.IDLE.value

        latest = _last_chats_updated(_drain(listener))
        assert latest is not None
        agents = [chat["active_agent"] for chat in latest["chats"]]
        assert isinstance(agents, list)
        assert agents[0]["activity_state"] == ActivityState.IDLE.value
    finally:
        agent_manager.stop()


def test_reset_activity_state_no_op_when_not_tracked(agent_manager: AgentManager) -> None:
    """reset_activity_state for an untracked agent is a quiet no-op with no cache residue."""
    agent_manager.reset_activity_state("ghost")
    with agent_manager._lock:
        assert "ghost" not in agent_manager._activity_state_by_agent
        assert "ghost" not in agent_manager._activity_tracker_by_agent


def test_stale_transcript_tail_after_restart_shows_idle(agent_manager: AgentManager, tmp_path: Path) -> None:
    """A running agent whose mid-turn transcript predates the current Claude
    process is shown IDLE, not "Thinking...".

    Reproduces the container-restart case: the transcript still ends on a
    tool_result from the turn that was abandoned when the restart killed Claude,
    so the running-but-idle agent would otherwise stay pinned at THINKING. Once
    mngr touches ``claude_process_started`` on resume, its newer mtime marks the
    tail as stale and the indicator settles on IDLE.
    """
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    agent_manager._ensure_activity_tracking("agent-1")

    # The transcript ends on a tool_result from the distant past (the abandoned
    # turn): an assistant tool_use matched by its tool_result, nothing after.
    agent_manager.update_session_events(
        "agent-1",
        [
            {
                "type": "assistant_message",
                "tool_calls": [{"tool_call_id": "call_a", "tool_name": "Bash"}],
                "timestamp": "2020-01-01T00:00:00.000Z",
            },
            {"type": "tool_result", "tool_call_id": "call_a", "timestamp": "2020-01-01T00:00:01.000Z"},
        ],
    )

    # Before the restart marker exists, the mid-turn tail still reads as THINKING.
    with agent_manager._lock:
        assert agent_manager._activity_state_by_agent["agent-1"] == ActivityState.THINKING

    # mngr touches claude_process_started on resume; its mtime ("now") is well
    # after the 2020 transcript events.
    (state_dir / "claude_process_started").touch()

    # In production the post-restart observe snapshot drives this recompute.
    agent_manager._recompute_activity_state("agent-1", broadcast_on_change=False)

    with agent_manager._lock:
        assert agent_manager._activity_state_by_agent["agent-1"] == ActivityState.IDLE
        assert agent_manager._agents["agent-1"].activity_state == ActivityState.IDLE.value


def test_codex_agent_gets_a_transcript_turn_latch_tracker(agent_manager: AgentManager, tmp_path: Path) -> None:
    """codex builds a transcript-derived tracker like claude/pi, but its dot is a latch on the
    transcript's real-time turn markers -- NOT the (laggy/unreliable) mngr lifecycle. So a RUNNING
    lifecycle with no open turn in the transcript reads IDLE, not THINKING. Its ledger stays for the
    queue; the daemon-less connection attempt here is a graceful no-op."""
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1", harness=HarnessType.CODEX, state="RUNNING")
    agent_manager._ensure_activity_tracking("agent-1")
    with agent_manager._lock:
        assert "agent-1" in agent_manager._activity_tracked_agents
        assert isinstance(agent_manager._activity_tracker_by_agent.get("agent-1"), CodexActivityTracker)
    # RUNNING lifecycle but no turn marker observed -> IDLE (the dot follows the transcript, not mngr).
    assert agent_manager._activity_state_by_agent.get("agent-1") == ActivityState.IDLE
    # A real-time turn_started marker lights it to THINKING.
    agent_manager.update_session_events(
        "agent-1", [{"type": SPECIAL_EVENT_TYPE, "kind": SpecialEventKind.TURN_STARTED.value}]
    )
    assert agent_manager._activity_state_by_agent.get("agent-1") == ActivityState.THINKING
    # No daemon in the test, so the session has no live connection to tap through.
    assert agent_manager._session_by_agent["agent-1"].is_tap_available(has_queued=True) is False


def test_stop_activity_tracking_clears_caches(agent_manager: AgentManager, tmp_path: Path) -> None:
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    agent_manager._ensure_activity_tracking("agent-1")
    # Seed a non-default cached state so we can verify it's cleared.
    agent_manager.update_session_events(
        "agent-1",
        [{"type": "user_message", "content": "go"}],
    )

    with agent_manager._lock:
        assert "agent-1" in agent_manager._activity_tracked_agents
        assert "agent-1" in agent_manager._activity_state_by_agent
        assert "agent-1" in agent_manager._activity_tracker_by_agent

    agent_manager._stop_activity_tracking("agent-1")

    with agent_manager._lock:
        assert "agent-1" not in agent_manager._activity_tracked_agents
        assert "agent-1" not in agent_manager._activity_state_by_agent
        assert "agent-1" not in agent_manager._activity_tracker_by_agent


def test_update_queued_messages_caches_broadcasts_and_serializes(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """A fresh queued snapshot from the watcher is cached, broadcast, and serialized."""
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    agent_manager._ensure_activity_tracking("agent-1")

    listener = broadcaster.register()
    try:
        snapshot = [
            {"queued_id": "q1", "content": "hello", "timestamp": "2026-08-07T00:00:01.000Z", "is_sending": False}
        ]
        agent_manager.update_queued_messages("agent-1", snapshot)

        with agent_manager._lock:
            assert agent_manager._agents["agent-1"].queued_messages == (
                QueuedMessageState(queued_id="q1", content="hello", timestamp="2026-08-07T00:00:01.000Z"),
            )
        latest = _last_chats_updated(_drain(listener))
        assert latest is not None
        agents = [chat["active_agent"] for chat in latest["chats"]]
        assert isinstance(agents, list)
        assert agents[0]["queued_messages"] == snapshot
        assert [q.model_dump() for q in agent_manager.get_chat_snapshots()[0].active_agent.queued_messages] == snapshot
    finally:
        agent_manager.stop()


def test_shoulder_tap_available_reflects_queue_and_send_in_flight(agent_manager: AgentManager, tmp_path: Path) -> None:
    """The derived ``shoulder_tap_available`` is true iff something is queued AND no send is in
    flight (contract Shoulder-tap), recomputed at each serialize from the two authoritative
    pieces of manager state -- never stored, so it cannot go stale."""
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    agent_manager._ensure_activity_tracking("agent-1")

    def available() -> bool:
        return agent_manager.get_chat_snapshots()[0].active_agent.shoulder_tap_available

    # Empty queue -> unavailable (nothing to tap).
    assert available() is False

    # Something queued, no send in flight -> available.
    agent_manager.update_queued_messages("agent-1", [{"queued_id": "q1", "content": "hi", "timestamp": "t"}])
    assert available() is True

    # A send in flight greys it, even with a non-empty queue (the manager consults the
    # session's Sending state, which the session's own send maintains around delivery).
    session = agent_manager._session_by_agent["agent-1"]
    assert isinstance(session, FileHarnessSession)
    session._sending.record("t1", "mid-flight")
    assert available() is False

    # Send resolved -> available again.
    session._sending.resolve("t1")
    assert available() is True

    agent_manager.stop()


def test_update_queued_messages_no_op_when_not_tracked(agent_manager: AgentManager) -> None:
    """Pushing a queued snapshot for an untracked agent leaves no cache residue."""
    agent_manager.update_queued_messages("ghost", [{"queued_id": "q", "content": "x", "timestamp": "t"}])
    with agent_manager._lock:
        assert "ghost" not in agent_manager._queued_messages_by_agent


def test_working_to_idle_drains_the_queue_via_the_registered_handler(
    agent_manager: AgentManager, tmp_path: Path
) -> None:
    """A working->IDLE transition invokes the watcher's queue backstop and clears the group."""
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    agent_manager._ensure_activity_tracking("agent-1")

    idle_calls: list[bool] = []

    def _drain_handler() -> list[dict[str, Any]]:
        idle_calls.append(True)
        return []

    agent_manager.register_queue_idle_handler("agent-1", _drain_handler)

    try:
        # A queued message is showing while the agent is thinking. The transcript goes
        # THINKING first: a snapshot arriving on an idle agent is swept at arrival by
        # ``update_queued_messages``'s pre-broadcast recompute, and in production the
        # enqueue only ever happens mid-turn.
        agent_manager.update_session_events("agent-1", [{"type": "user_message", "content": "go"}])
        agent_manager.update_queued_messages("agent-1", [{"queued_id": "q1", "content": "hi", "timestamp": "t"}])
        with agent_manager._lock:
            assert agent_manager._activity_state_by_agent["agent-1"] == ActivityState.THINKING
            assert len(agent_manager._agents["agent-1"].queued_messages) == 1

        # The turn ends (assistant reply, no pending tools) -> IDLE, so the backstop fires.
        agent_manager.update_session_events(
            "agent-1",
            [{"type": "user_message", "content": "go"}, {"type": "assistant_message", "tool_calls": []}],
        )
        with agent_manager._lock:
            assert agent_manager._activity_state_by_agent["agent-1"] == ActivityState.IDLE
            assert agent_manager._agents["agent-1"].queued_messages == ()
        assert idle_calls == [True]
    finally:
        agent_manager.stop()


def test_idle_agent_with_a_stale_queue_is_swept_without_a_transition(
    agent_manager: AgentManager, tmp_path: Path
) -> None:
    """The backstop is level-triggered: an already-IDLE agent that shows a queued
    survivor is swept on the next recompute, even with no working->IDLE edge.

    The survivor is seeded straight into the caches -- the shape of residue that
    reached the manager with no trigger having run (a snapshot arriving through
    ``update_queued_messages`` is already swept at arrival by its own pre-broadcast
    recompute, covered separately)."""
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    # _ensure_activity_tracking seeds IDLE with no working->IDLE transition.
    agent_manager._ensure_activity_tracking("agent-1")

    idle_calls: list[bool] = []

    def _drain_handler() -> list[dict[str, Any]]:
        idle_calls.append(True)
        return []

    agent_manager.register_queue_idle_handler("agent-1", _drain_handler)
    try:
        with agent_manager._lock:
            assert agent_manager._activity_state_by_agent["agent-1"] == ActivityState.IDLE
            # A stale queued entry is showing on the idle agent (no turn in flight).
            stale = (QueuedMessageState(queued_id="q1", content="stale", timestamp="t"),)
            agent_manager._queued_messages_by_agent["agent-1"] = stale
            agent_state = agent_manager._agents["agent-1"]
            agent_manager._agents["agent-1"] = agent_state.model_copy_update(
                to_update(agent_state.field_ref().queued_messages, stale)
            )

        # A plain recompute (agent still IDLE, no edge) must sweep it.
        agent_manager._recompute_activity_state("agent-1", broadcast_on_change=False)
        with agent_manager._lock:
            assert agent_manager._agents["agent-1"].queued_messages == ()
        assert idle_calls == [True]
    finally:
        agent_manager.stop()


def test_queued_snapshot_arriving_while_idle_is_swept_before_broadcast(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """A non-empty queued snapshot arriving while the derived state is IDLE (e.g.
    the priming replay resurrecting a dead process's dangling enqueues for a
    stopped agent) triggers the sweep at arrival, and the broadcast carries the
    drained snapshot -- the phantoms are never rendered."""
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    agent_manager._ensure_activity_tracking("agent-1")

    idle_calls: list[bool] = []

    def _drain_handler() -> list[dict[str, Any]]:
        idle_calls.append(True)
        return []

    agent_manager.register_queue_idle_handler("agent-1", _drain_handler)
    listener = broadcaster.register()
    try:
        agent_manager.update_queued_messages("agent-1", [{"queued_id": "q1", "content": "ghost", "timestamp": "t"}])

        with agent_manager._lock:
            assert agent_manager._agents["agent-1"].queued_messages == ()
        assert idle_calls == [True]
        # The arrival still broadcasts, and no broadcast ever carried the phantom.
        updates = [m for m in _drain(listener) if m.get("type") == "chats_updated"]
        assert updates
        for update in updates:
            assert update["chats"][0]["active_agent"]["queued_messages"] == []
    finally:
        agent_manager.stop()


def test_stopped_codex_agent_snapshot_is_swept_before_any_broadcast(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """A codex agent whose daemon generation died drops any cached queue chips before broadcast.

    codex's queue is EPHEMERAL and lives with its live ledger; an abrupt daemon kill emits no idle
    sweep, so the dead-lifecycle recompute is what clears the cached chips and settles the dot to
    IDLE. No broadcast ever contains the phantoms.
    """
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1", harness=HarnessType.CODEX, state="STOPPED")
    agent_manager._ensure_activity_tracking("agent-1")

    try:
        listener = broadcaster.register()
        # The dead generation's orphan chips arrive from a late snapshot push.
        agent_manager.update_queued_messages("agent-1", [{"queued_id": "q1", "content": "phantom", "timestamp": "t"}])

        messages = _drain(listener)
        updates = [message for message in messages if message.get("type") == "chats_updated"]
        assert updates, "the snapshot arrival still broadcasts (the swept state)"
        for update in updates:
            assert update["chats"][0]["active_agent"]["queued_messages"] == []
        assert updates[-1]["chats"][0]["active_agent"]["activity_state"] == ActivityState.IDLE.value
        with agent_manager._lock:
            assert agent_manager._agents["agent-1"].queued_messages == ()
    finally:
        agent_manager.stop()


def test_queued_snapshot_arriving_mid_turn_is_kept(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """The same snapshot arriving with seeded mid-turn signals (derive non-IDLE)
    is kept: the pre-broadcast sweep only drains an idle agent's queue, so a live
    agent's genuine mirror survives a backend-restart replay."""
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    agent_manager._ensure_activity_tracking("agent-1")

    idle_calls: list[bool] = []

    def _drain_handler() -> list[dict[str, Any]]:
        idle_calls.append(True)
        return []

    agent_manager.register_queue_idle_handler("agent-1", _drain_handler)
    # Seeded mid-turn signals: a user_message at the tail derives THINKING.
    agent_manager.update_session_events("agent-1", [{"type": "user_message", "content": "go"}])
    listener = broadcaster.register()
    try:
        snapshot = [{"queued_id": "q1", "content": "parked", "timestamp": "t", "is_sending": False}]
        agent_manager.update_queued_messages("agent-1", snapshot)

        with agent_manager._lock:
            assert agent_manager._agents["agent-1"].queued_messages == (
                QueuedMessageState(queued_id="q1", content="parked", timestamp="t"),
            )
        assert idle_calls == []
        latest = _last_chats_updated(_drain(listener))
        assert latest is not None
        agents = [chat["active_agent"] for chat in latest["chats"]]
        assert isinstance(agents, list)
        assert agents[0]["queued_messages"] == snapshot
    finally:
        agent_manager.stop()


def test_unknown_lifecycle_codex_keeps_its_queued_snapshot(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """UNKNOWN is non-evidence (an unreachable provider, not a death): a codex agent's queued snapshot
    survives it -- the queue clear only fires on a positively-dead state. The dot, now lifecycle-driven
    like claude, cannot confirm a live turn under UNKNOWN (codex has no ``active`` marker), so it reads
    IDLE, while the queue is left untouched."""
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1", harness=HarnessType.CODEX, state="UNKNOWN")
    agent_manager._ensure_activity_tracking("agent-1")

    try:
        listener = broadcaster.register()
        snapshot = [{"queued_id": "q1", "content": "still parked", "timestamp": "t", "is_sending": False}]
        agent_manager.update_queued_messages("agent-1", snapshot)

        latest = _last_chats_updated(_drain(listener))
        assert latest is not None
        assert latest["chats"][0]["active_agent"]["queued_messages"] == snapshot
        assert latest["chats"][0]["active_agent"]["activity_state"] == ActivityState.IDLE.value
        with agent_manager._lock:
            assert len(agent_manager._agents["agent-1"].queued_messages) == 1
    finally:
        agent_manager.stop()


def test_running_mid_turn_codex_snapshot_passes_through_unchanged(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """A codex agent mid-turn (an open turn in the transcript -> THINKING) keeps its queued snapshot:
    a non-dead agent that is working never triggers the idle stale-queue sweep, so the broadcast
    carries the chips."""
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1", harness=HarnessType.CODEX, state="RUNNING")
    agent_manager._ensure_activity_tracking("agent-1")

    try:
        # Mid-turn = the transcript's latest marker is turn_started -> the dot latches to THINKING.
        agent_manager.update_session_events(
            "agent-1", [{"type": SPECIAL_EVENT_TYPE, "kind": SpecialEventKind.TURN_STARTED.value}]
        )
        listener = broadcaster.register()
        snapshot = [{"queued_id": "q1", "content": "queued mid-turn", "timestamp": "t", "is_sending": False}]
        agent_manager.update_queued_messages("agent-1", snapshot)

        latest = _last_chats_updated(_drain(listener))
        assert latest is not None
        assert latest["chats"][0]["active_agent"]["queued_messages"] == snapshot
        assert latest["chats"][0]["active_agent"]["activity_state"] == ActivityState.THINKING.value
        with agent_manager._lock:
            assert len(agent_manager._agents["agent-1"].queued_messages) == 1
    finally:
        agent_manager.stop()


def test_stop_activity_tracking_clears_queued_caches(agent_manager: AgentManager, tmp_path: Path) -> None:
    """Stopping tracking drops the queued snapshot and idle handler alongside activity state."""
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    agent_manager._ensure_activity_tracking("agent-1")
    agent_manager.register_queue_idle_handler("agent-1", lambda: [])
    agent_manager.update_queued_messages("agent-1", [{"queued_id": "q1", "content": "hi", "timestamp": "t"}])

    with agent_manager._lock:
        assert "agent-1" in agent_manager._queued_messages_by_agent
        assert "agent-1" in agent_manager._queue_idle_handler_by_agent

    agent_manager._stop_activity_tracking("agent-1")

    with agent_manager._lock:
        assert "agent-1" not in agent_manager._queued_messages_by_agent
        assert "agent-1" not in agent_manager._queue_idle_handler_by_agent


def test_provider_snapshot_preserves_queued_messages_for_tracked_agent(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """A re-listing observe snapshot must not wipe an already-tracked agent's queued group."""
    test_agent_id = MngrAgentId()
    str_id = str(test_agent_id)

    state_dir = tmp_path / "agents" / str_id
    state_dir.mkdir(parents=True)

    agent = _agent_details("snapshot-agent", agent_id=test_agent_id, work_dir=str(tmp_path / "work"))
    agent_manager._handle_observe_event(make_agent_state_event(agent))
    agent_manager.update_queued_messages(str_id, [{"queued_id": "q1", "content": "hi", "timestamp": "t"}])

    listener = broadcaster.register()
    try:
        agent_manager._handle_observe_event(make_full_agent_state_event([agent]))
        latest = _last_chats_updated(_drain(listener))
        assert latest is not None
        agents = [chat["active_agent"] for chat in latest["chats"]]
        assert isinstance(agents, list)
        assert agents[0]["agent_id"] == str_id
        assert agents[0]["queued_messages"] == [
            {"queued_id": "q1", "content": "hi", "timestamp": "t", "is_sending": False}
        ]
    finally:
        agent_manager.stop()


def test_agent_removed_event_fires_removal_side_effects(agent_manager: AgentManager, tmp_path: Path) -> None:
    """An AGENT_REMOVED event drops the agent and clears its activity tracking and caches."""
    test_agent_id = MngrAgentId()
    str_id = str(test_agent_id)

    state_dir = tmp_path / "agents" / str_id
    state_dir.mkdir(parents=True)
    agent = _agent_details("to-destroy", agent_id=test_agent_id)
    agent_manager._handle_observe_event(make_agent_state_event(agent))
    with agent_manager._lock:
        assert str_id in agent_manager._activity_tracked_agents

    agent_manager._handle_observe_event(make_agent_removed_event(agent.id, agent.name, agent.host.id))

    assert agent_manager.get_agent_by_id(str_id) is None
    with agent_manager._lock:
        assert str_id not in agent_manager._activity_tracked_agents
        assert str_id not in agent_manager._activity_state_by_agent


def test_agent_removed_event_drops_pending_permissions_and_presence(
    agent_manager: AgentManager, tmp_path: Path
) -> None:
    """The observe path forgets the same per-agent state ``remove_agent`` does: a chat destroyed
    from the CLI must not keep a pending permission request or an open presence report."""
    test_agent_id = MngrAgentId()
    str_id = str(test_agent_id)
    (tmp_path / "agents" / str_id).mkdir(parents=True)
    agent = _agent_details("to-destroy", agent_id=test_agent_id)
    agent_manager._handle_observe_event(make_agent_state_event(agent))
    with agent_manager._lock:
        agent_manager._pending_permission_ids_by_agent[str_id] = {"evt-1"}
    agent_manager.record_presence(ChatId(str_id), "client-1", PresenceState.VISIBLE)
    assert agent_manager.has_pending_permission(ChatId(str_id))
    assert agent_manager._oom_prioritizer._presence.is_open(ChatId(str_id))

    agent_manager._handle_observe_event(make_agent_removed_event(agent.id, agent.name, agent.host.id))

    assert not agent_manager.has_pending_permission(ChatId(str_id))
    assert not agent_manager._oom_prioritizer._presence.is_open(ChatId(str_id))


def test_provider_snapshot_preserves_activity_state_for_tracked_agent(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """A per-provider snapshot must not wipe the activity_state of agents that
    are already being tracked for activity.

    Regression test: ``_handle_observe_event`` rebuilds ``_agents`` wholesale
    from the raw observe payload (which has no ``activity_state`` field) on every
    event. Only ids in the membership delta's ``added`` set get an
    ``_ensure_activity_tracking`` recompute, so a snapshot that merely re-lists an
    already-known agent reports it in neither add nor remove. Without re-applying
    the cached state, the broadcast that follows would emit ``activity_state=None``
    for every previously-tracked agent and the chat panel indicator would briefly
    disappear.
    """
    test_agent_id = MngrAgentId()
    str_id = str(test_agent_id)

    state_dir = tmp_path / "agents" / str_id
    state_dir.mkdir(parents=True)

    # First, simulate the agent already being tracked with a live watcher
    # whose transcript signals THINKING (a user_message with no reply).
    agent = _agent_details("snapshot-agent", agent_id=test_agent_id, work_dir=str(tmp_path / "work"))
    agent_manager._handle_observe_event(make_agent_state_event(agent))
    agent_manager.update_session_events(str_id, [{"type": "user_message", "content": "go"}])
    with agent_manager._lock:
        assert agent_manager._activity_state_by_agent[str_id] == ActivityState.THINKING
        assert agent_manager._agents[str_id].activity_state == ActivityState.THINKING.value

    # Now drain prior broadcasts so the snapshot's broadcast is the only one
    # we read.
    listener = broadcaster.register()
    try:
        snapshot_event = make_full_agent_state_event([agent])
        agent_manager._handle_observe_event(snapshot_event)

        latest = _last_chats_updated(_drain(listener))
        assert latest is not None
        agents = [chat["active_agent"] for chat in latest["chats"]]
        assert isinstance(agents, list)
        # The broadcast must carry the cached activity_state, not None.
        assert agents[0]["agent_id"] == str_id
        assert agents[0]["activity_state"] == ActivityState.THINKING.value

        with agent_manager._lock:
            assert agent_manager._agents[str_id].activity_state == ActivityState.THINKING.value
    finally:
        agent_manager.stop()


def test_agent_state_event_stopped_flips_lifecycle_and_activity_to_idle(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """A STOPPED AGENT_STATE event for a tracked, thinking agent broadcasts state=STOPPED
    and re-gates its activity indicator to IDLE.

    The observe stream now carries each agent's real lifecycle state, so an agent
    whose process dies on its own arrives as STOPPED. Because STOPPED is not in
    ``RUNNING_LIFECYCLE_STATES``, the recompute pass must settle its activity to
    IDLE even though the transcript tail still reads THINKING.
    """
    test_agent_id = MngrAgentId()
    str_id = str(test_agent_id)

    state_dir = tmp_path / "agents" / str_id
    state_dir.mkdir(parents=True)

    running = _agent_details("dying-agent", agent_id=test_agent_id, state=AgentLifecycleState.RUNNING)
    agent_manager._handle_observe_event(make_agent_state_event(running))
    # A pending user_message with no reply pins the transcript-derived state at THINKING.
    agent_manager.update_session_events(str_id, [{"type": "user_message", "content": "go"}])
    with agent_manager._lock:
        assert agent_manager._activity_state_by_agent[str_id] == ActivityState.THINKING

    listener = broadcaster.register()
    try:
        stopped = _agent_details("dying-agent", agent_id=test_agent_id, state=AgentLifecycleState.STOPPED)
        agent_manager._handle_observe_event(make_agent_state_event(stopped))

        latest = _last_chats_updated(_drain(listener))
        assert latest is not None
        agents = [chat["active_agent"] for chat in latest["chats"]]
        assert isinstance(agents, list)
        assert agents[0]["agent_id"] == str_id
        assert agents[0]["state"] == AgentLifecycleState.STOPPED.value
        assert agents[0]["activity_state"] == ActivityState.IDLE.value

        with agent_manager._lock:
            assert agent_manager._agents[str_id].state == AgentLifecycleState.STOPPED.value
            assert agent_manager._activity_state_by_agent[str_id] == ActivityState.IDLE
    finally:
        agent_manager.stop()


def test_full_snapshot_rebuilds_agent_set_and_broadcasts(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster
) -> None:
    """A full snapshot rebuilds the tracked set: new agents appear, absent ones are dropped,
    and a single chats_updated broadcast reflects the rebuilt set."""
    first = _agent_details("first-agent")
    agent_manager._handle_observe_event(make_full_agent_state_event([first]))
    assert {a.id for a in agent_manager.get_agents()} == {str(first.id)}

    q = broadcaster.register()
    second = _agent_details("second-agent")
    agent_manager._handle_observe_event(make_full_agent_state_event([second]))

    tracked_ids = {a.id for a in agent_manager.get_agents()}
    assert tracked_ids == {str(second.id)}
    assert str(first.id) not in tracked_ids

    raw = q.get_nowait()
    assert raw is not None
    msg = json.loads(raw)
    assert msg["type"] == "chats_updated"
    assert {chat["chat_id"] for chat in msg["chats"]} == {str(second.id)}


# =============================================================================
# Offline codex model-chip resolution from the persisted raw model-list sidecar
# =============================================================================


def _codex_model_entry(model: str, effort: str, *, priority: bool = False) -> CodexModel:
    """A ``model/list`` entry for the sidecar tests (id == model)."""
    return CodexModel.model_validate(
        {
            "id": model,
            "model": model,
            "displayName": model.upper(),
            "supportedReasoningEfforts": [{"reasoningEffort": effort}],
            "serviceTiers": [{"id": "priority"}] if priority else [],
        }
    )


def test_codex_model_options_is_none_without_a_cache_or_a_sidecar(agent_manager: AgentManager) -> None:
    # No in-memory set and no sidecar on disk -> empty (the chip renders nothing).
    _seed_agent(agent_manager, "agent-1", harness=HarnessType.CODEX)
    session = agent_manager._build_session("agent-1", HarnessType.CODEX)
    assert session.switch_options() == ()


def test_codex_model_options_falls_back_to_the_sidecar_when_the_cache_is_empty(
    agent_manager: AgentManager,
) -> None:
    # Post-restart: the in-memory set is empty, so the option set is mapped from the persisted raw
    # sidecar -- the whole point of the fix (the chip resolves before the daemon reconnects).
    _seed_agent(agent_manager, "agent-1", harness=HarnessType.CODEX)
    models = (_codex_model_entry("gpt-5.6-terra", "high", priority=True),)
    write_codex_model_options(get_codex_model_options_path(agent_manager._get_agent_state_dir("agent-1")), models)
    session = agent_manager._build_session("agent-1", HarnessType.CODEX)
    options = session.switch_options()
    assert [opt.id for opt in options] == ["gpt-5.6-terra"]


def test_codex_model_options_in_memory_cache_wins_over_the_sidecar(agent_manager: AgentManager) -> None:
    # Precedence: a live in-memory set always supersedes the on-disk fallback, so a reconnect's fresh
    # list is authoritative even when a (stale) sidecar exists.
    _seed_agent(agent_manager, "agent-1", harness=HarnessType.CODEX)
    stale = (_codex_model_entry("gpt-old", "high"),)
    write_codex_model_options(get_codex_model_options_path(agent_manager._get_agent_state_dir("agent-1")), stale)
    live = codex_models_to_options((_codex_model_entry("gpt-5.6-terra", "high"),))
    session = agent_manager._build_session("agent-1", HarnessType.CODEX)
    session.note_offered_options(live)
    options = session.switch_options()
    assert [opt.id for opt in options] == ["gpt-5.6-terra"]


def test_offline_codex_chip_matches_the_persisted_selection_from_the_sidecar(agent_manager: AgentManager) -> None:
    # The end-to-end offline path: a valid persisted selection plus the raw sidecar (and no live
    # daemon / empty in-memory set) resolves the chip to the real model -- not the "unrecognized
    # model" shrug (matched is None).
    agent_id = "agent-1"
    _seed_agent(agent_manager, agent_id, harness=HarnessType.CODEX)
    state_dir = agent_manager._get_agent_state_dir(agent_id)
    state_path = get_model_state_path(HarnessType.CODEX, state_dir)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"model": "gpt-5.6-terra", "effort": "high", "fast": False}))
    write_codex_model_options(
        get_codex_model_options_path(state_dir), (_codex_model_entry("gpt-5.6-terra", "high", priority=True),)
    )

    # Without the sidecar the identity would match nothing (the pre-fix shrug); with it, the chip resolves.
    agent_manager._session_by_agent[agent_id] = agent_manager._build_session(agent_id, HarnessType.CODEX)
    assert agent_manager._session_by_agent[agent_id].switch_options() != ()
    agent_manager._recompute_model_choice(agent_id, broadcast_on_change=False)
    choice = agent_manager._agents[agent_id].model_choice
    assert choice is not None
    assert choice.identity.model_id == "gpt-5.6-terra"
    assert choice.matched is not None
    assert choice.matched.id == "gpt-5.6-terra"


def _capture_prioritizer_writes(manager: AgentManager, pids: dict[str, int]) -> list[tuple[int, int]]:
    """Swap in an OOM prioritizer that captures its band writes, and return the log.

    Wired to the manager's own ``get_chat_ids`` / ``_read_process_started_at``
    (the collaborators under test) but to a fake pid resolver and a capturing
    ``set_adj``, so the manager's real seeding and lifecycle paths are exercised
    without touching ``/proc``.
    """
    writes: list[tuple[int, int]] = []
    manager._oom_prioritizer = ChatOomPrioritizer(
        list_chat_ids=manager.get_chat_ids,
        resolve_pid=lambda cid: pids.get(cid),
        set_adj=lambda pid, adj: (writes.append((pid, adj)), True)[1],
        resolve_process_started_at=manager._read_agent_process_started_at,
    )
    return writes


def test_seeding_recovers_chat_message_recency_from_the_message_stamps(
    broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """A restarted chat app recovers which chats were recently messaged.

    The prioritizer's recency state is in-memory, so on restart it is re-seeded
    from the durable message stamps. Without that, every chat would look
    never-messaged and start aging from its process-start time.
    """
    stamps_path = tmp_path / "last_messaged.json"
    older = _agent_details("older-chat", labels={"user_created": "true"})
    newer = _agent_details("newer-chat", labels={"user_created": "true"})
    now = time.time()
    previous_run = MessageStampStore(path=stamps_path)
    previous_run.record(ChatId(older.id), at=now - 60 * 60)
    previous_run.record(ChatId(newer.id), at=now - 30 * 60)

    manager = AgentManager.build(broadcaster, message_stamps=MessageStampStore(path=stamps_path))
    try:
        manager._handle_observe_event(make_full_agent_state_event([older, newer]))
        writes = _capture_prioritizer_writes(manager, {str(older.id): 10, str(newer.id): 20})
        manager._seed_oom_prioritizer()
        manager._oom_prioritizer.reapply()
    finally:
        manager.stop()

    latest = {pid: adj for pid, adj in writes}
    # The more recently messaged chat outranks the other, which only holds if the
    # stamps were found and ordered.
    assert latest[20] < latest[10]


def test_message_stamps_follow_the_chats_they_stamp(broadcaster: WebSocketBroadcaster, tmp_path: Path) -> None:
    """A send stamps the chat on disk, and a destroyed chat's stamp goes with it."""
    stamps_path = tmp_path / "last_messaged.json"
    chat = _agent_details("chat", labels={"user_created": "true"})
    manager = AgentManager.build(broadcaster, message_stamps=MessageStampStore(path=stamps_path))
    try:
        manager._handle_observe_event(make_full_agent_state_event([chat]))
        manager.record_message_sent(ChatId(str(chat.id)))
        assert set(MessageStampStore(path=stamps_path).read()) == {str(chat.id)}
        manager.remove_agent(str(chat.id))
        assert MessageStampStore(path=stamps_path).read() == {}
    finally:
        manager.stop()


def _age_process_start_marker(manager: AgentManager, agent_id: str, seconds_ago: float) -> None:
    """Backdate the agent's ``claude_process_started`` marker, so it reads as idle."""
    state_dir = manager._get_agent_state_dir(agent_id)
    state_dir.mkdir(parents=True, exist_ok=True)
    marker = state_dir / "claude_process_started"
    marker.touch()
    old = time.time() - seconds_ago
    os.utime(marker, (old, old))


def test_observe_events_exempt_a_running_chat_from_aging_out(agent_manager: AgentManager) -> None:
    """A chat mid-turn stays below the worker band however long it has been idle.

    The observe stream is the prioritizer's only view of a chat messaged outside
    the workspace UI, so this is what keeps such a chat -- e.g. one running a long
    task another agent kicked off -- from being shed mid-task.
    """
    chat = _agent_details("busy-chat", labels={"user_created": "true"}, state=AgentLifecycleState.RUNNING)
    chat_id = str(chat.id)
    agent_manager._handle_observe_event(make_full_agent_state_event([chat]))
    _age_process_start_marker(agent_manager, chat_id, seconds_ago=3 * 24 * 3600)
    writes = _capture_prioritizer_writes(agent_manager, {chat_id: 10})

    agent_manager._handle_observe_event(make_full_agent_state_event([chat]))
    assert writes, "a running chat should have been re-tagged from the observe event"
    assert writes[-1][1] < bands.WORKER_AGENT

    # The turn ends; the chat is no longer exempt, but the turn's end counts as
    # engagement, so it resumes aging from now rather than from three days ago.
    stopped = _agent_details(
        "busy-chat", agent_id=chat.id, labels={"user_created": "true"}, state=AgentLifecycleState.WAITING
    )
    agent_manager._handle_observe_event(make_full_agent_state_event([stopped]))
    assert writes[-1][1] == bands.CHAT_AGENT_BASE


def test_session_cache_heals_when_the_real_harness_arrives(agent_manager: AgentManager, tmp_path: Path) -> None:
    """Tracking can start before observe reports the agent (the create path), caching a
    DEFAULT-harness session; the first caller that knows the real harness must replace it,
    or a codex agent would send through mngr's file API forever."""
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    # Tracking starts with no _agents entry -> the claude default guess.
    agent_manager._ensure_activity_tracking("agent-1")
    assert agent_manager._session_by_agent["agent-1"].harness is HarnessType.CLAUDE
    # The observe stream catches up: the agent is codex; re-tracking heals both caches.
    _seed_agent(agent_manager, "agent-1", harness=HarnessType.CODEX)
    agent_manager._ensure_activity_tracking("agent-1")
    assert agent_manager._session_by_agent["agent-1"].harness is HarnessType.CODEX
    assert isinstance(agent_manager._activity_tracker_by_agent["agent-1"], CodexActivityTracker)


def test_stop_activity_tracking_keeps_the_sending_records(agent_manager: AgentManager, tmp_path: Path) -> None:
    """A transient discovery blip quiesces the session without destroying it: an in-flight
    Sending record must survive so a stop after the blip still returns the text (A4) --
    the same lifetime the watcher registry has."""
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    agent_manager._ensure_activity_tracking("agent-1")
    session = agent_manager._session_by_agent["agent-1"]
    assert isinstance(session, FileHarnessSession)
    session._sending.record("t-inflight", "caught mid-send")

    agent_manager._stop_activity_tracking("agent-1")
    assert agent_manager._session_by_agent["agent-1"] is session
    assert session.in_flight_block() == "caught mid-send"


# --- Watcher eviction (the chat-memory lifecycle) ---


def test_remove_agent_evicts_the_watcher(agent_manager: AgentManager) -> None:
    """A destroyed agent's watcher is evicted along with its tracking state."""
    evicted: list[str] = []
    agent_manager.set_watcher_eviction_callback(evicted.append)
    agent = _agent_details("doomed-agent")
    agent_manager._handle_observe_event(make_agent_state_event(agent))

    agent_manager.remove_agent(str(agent.id))
    assert evicted == [str(agent.id)]


def test_lifecycle_transition_into_dead_evicts_the_watcher_once(agent_manager: AgentManager) -> None:
    """Eviction is edge-triggered on the transition into a positively-dead lifecycle: a
    stop (from the UI, mngr, an OOM shed, idle shutdown) drops the resident transcript,
    while further observe ticks of the already-stopped agent do NOT re-evict -- a user
    viewing a stopped chat's history rebuilds the watcher on read, and a level-triggered
    evict would tear that rebuild down again every tick."""
    evicted: list[str] = []
    agent_manager.set_watcher_eviction_callback(evicted.append)
    agent = _agent_details("stoppable-agent")
    agent_manager._handle_observe_event(make_agent_state_event(agent))
    assert evicted == []

    stopped = agent.model_copy_update(to_update(agent.field_ref().state, AgentLifecycleState.STOPPED))
    agent_manager._handle_observe_event(make_agent_state_event(stopped))
    assert evicted == [str(agent.id)]

    # Another tick of the same dead state: no edge, no eviction.
    agent_manager._handle_observe_event(make_agent_state_event(stopped))
    assert evicted == [str(agent.id)]

    # A restart followed by another stop evicts again.
    running = agent.model_copy_update(to_update(agent.field_ref().state, AgentLifecycleState.RUNNING))
    agent_manager._handle_observe_event(make_agent_state_event(running))
    agent_manager._handle_observe_event(make_agent_state_event(stopped))
    assert evicted == [str(agent.id), str(agent.id)]


def test_note_agent_alive_flips_a_dead_state_to_waiting(agent_manager: AgentManager) -> None:
    """After this server starts an agent itself, the tracked state reflects the revival
    immediately: the observe stream only notices a revival on its five-minute full
    snapshot (a stopped agent has no pid to watch), which left the chat reading dead
    for minutes while the agent was demonstrably up."""
    _seed_agent(agent_manager, "revived", state="DONE")
    agent_manager.note_agent_alive("revived")
    revived = agent_manager.get_agent_by_id("revived")
    assert revived is not None
    assert revived.state == "WAITING"
    assert agent_manager.is_agent_alive("revived")


def test_note_agent_alive_leaves_live_and_unknown_states_alone(agent_manager: AgentManager) -> None:
    """Only a positively-dead state is corrected: RUNNING must not be demoted to WAITING
    (the fold would lose a turn in flight), UNKNOWN is non-evidence, and an untracked id
    is not something to invent a record for."""
    _seed_agent(agent_manager, "busy", state="RUNNING")
    agent_manager.note_agent_alive("busy")
    busy = agent_manager.get_agent_by_id("busy")
    assert busy is not None
    assert busy.state == "RUNNING"

    _seed_agent(agent_manager, "unseen", state="UNKNOWN")
    agent_manager.note_agent_alive("unseen")
    unseen = agent_manager.get_agent_by_id("unseen")
    assert unseen is not None
    assert unseen.state == "UNKNOWN"

    agent_manager.note_agent_alive("never-tracked")
    assert agent_manager.get_agent_by_id("never-tracked") is None


def test_a_filed_permission_request_is_pending_until_its_verdict_lands(
    agent_manager: AgentManager, tmp_path: Path
) -> None:
    """The chat row's ``attention`` status: a filed request with no resolution yet."""
    (tmp_path / "agents" / "agent-1").mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    agent_manager._ensure_activity_tracking("agent-1")
    nudger = RecordingNudger()
    agent_manager.set_nudger(nudger)
    try:
        agent_manager.update_session_events(
            "agent-1",
            [{"type": "tool_result", "tool_call_id": "x", "permission_request": {"request_id": "evt-1"}}],
        )
        assert agent_manager.has_pending_permission(ChatId("agent-1"))
        nudges_after_filing = nudger.nudge_count
        assert nudges_after_filing >= 1

        agent_manager.update_session_events(
            "agent-1",
            [
                {
                    "type": "user_message",
                    "content": "(resolution: granted, request_id: evt-1)",
                    "display": "permission_resolution",
                    "request_id": "evt-1",
                }
            ],
        )
        assert not agent_manager.has_pending_permission(ChatId("agent-1"))
        assert nudger.nudge_count > nudges_after_filing
    finally:
        agent_manager.stop()


def test_a_result_without_a_filed_request_leaves_nothing_pending(agent_manager: AgentManager, tmp_path: Path) -> None:
    (tmp_path / "agents" / "agent-1").mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    agent_manager._ensure_activity_tracking("agent-1")
    try:
        agent_manager.update_session_events("agent-1", [{"type": "tool_result", "tool_call_id": "x"}])
        assert not agent_manager.has_pending_permission(ChatId("agent-1"))
    finally:
        agent_manager.stop()


def test_the_agent_list_is_known_after_the_first_full_snapshot(agent_manager: AgentManager) -> None:
    assert not agent_manager.is_agent_list_known()
    agent_manager._handle_observe_event(make_full_agent_state_event([]))
    assert agent_manager.is_agent_list_known()


def test_every_agent_list_broadcast_nudges_the_shell(agent_manager: AgentManager) -> None:
    nudger = RecordingNudger()
    agent_manager.set_nudger(nudger)
    agent_manager._handle_observe_event(make_full_agent_state_event([_agent_details("chat-1")]))
    assert nudger.nudge_count == 1
    agent_manager.remove_agent(next(iter(agent_manager.get_agents())).id)
    assert nudger.nudge_count == 2


# --- The auto-open reactor, fed from the observe stream ---


def test_observe_events_feed_the_auto_open_reactor(
    broadcaster: WebSocketBroadcaster, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The first listing seeds (a labeled chat the ledger does not name is owed its tab), every
    labeled agent that appears afterwards is opened, and a removed one is forgotten."""
    monkeypatch.setenv("MNGR_AGENT_ID", "test-agent-id")
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", "/tmp/test-work")
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    shell = RecordingShell(client_ids=["c1"])
    reactor = AutoOpenReactor(ledger=AutoOpenLedger(path=None), shell=shell)
    manager = AgentManager.build(broadcaster, auto_open=reactor)
    at_start = _agent_details("update-self-1", labels={"auto_open": "true"})
    plain = _agent_details("chat-1", labels={"user_created": "true"})

    manager._handle_observe_event(make_full_agent_state_event([at_start, plain]))
    reactor.flush()

    assert shell.opens == [(str(at_start.id), "c1")]
    assert not reactor.ledger.is_delivered(ChatId(plain.id))

    appeared = _agent_details("assist-new", labels={"assist": "true", "auto_open": "true"})
    manager._handle_observe_event(make_agent_state_event(appeared))
    reactor.flush()
    assert shell.opens[-1] == (str(appeared.id), "c1")
    assert reactor.ledger.is_delivered(ChatId(appeared.id))

    manager._handle_observe_event(make_agent_removed_event(appeared.id, appeared.name, appeared.host.id))
    assert not reactor.ledger.is_delivered(ChatId(appeared.id))


# --- Chats that have run on several agents (a hand-built record) ---


class _UnremovableChatRecordStore(InMemoryChatRecordStore):
    """A store whose records cannot be deleted: what a read-only chat folder looks like to the file store."""

    def delete(self, chat_id: ChatId) -> None:
        raise ChatRecordError(f"chat record folder for {chat_id} could not be removed")


def _recorded_chat(
    broadcaster: WebSocketBroadcaster,
    mngr_binary: str | None = None,
    store: InMemoryChatRecordStore | None = None,
) -> tuple[AgentManager, InMemoryChatRecordStore, str, str]:
    """A manager tracking a chat that moved from ``first`` (archived, stopped) to ``second`` (running)."""
    store = store if store is not None else InMemoryChatRecordStore()
    manager = AgentManager.build(
        broadcaster, chat_record_store=store, mngr_binary=mngr_binary if mngr_binary is not None else "mngr"
    )
    first, second = f"agent-{uuid4().hex}", f"agent-{uuid4().hex}"
    seed_agent_state(
        manager,
        first,
        name=f"archived-1-Chat-1-{first}",
        state="STOPPED",
        labels={
            "account": "acct-1",
            "archived_at": "2026-09-01T13:01:00+00:00",
            "display_name": "Chat 1 (archived 1)",
        },
    )
    seed_agent_state(
        manager,
        second,
        name="Chat-1",
        labels={"account": "acct-2", "display_name": "Chat 1", "chat_id": first, "chat_seq": "2"},
        harness=HarnessType.CODEX,
    )
    store.write(make_two_member_chat_record(first, second))
    manager.refresh_chat_records()
    return manager, store, first, second


def test_a_recorded_chat_lists_once_under_its_first_agent_with_its_members_in_order(
    broadcaster: WebSocketBroadcaster,
) -> None:
    manager, _store, first, second = _recorded_chat(broadcaster)
    try:
        snapshots = manager.get_chat_snapshots()
        assert [snapshot.chat_id for snapshot in snapshots] == [first]
        (snapshot,) = snapshots
        assert snapshot.agent_ids == (first, second)
        assert snapshot.active_agent.agent_id == second
        assert snapshot.active_agent.harness is HarnessType.CODEX
        assert (snapshot.name, snapshot.title) == ("Chat-1", "Chat 1")
        assert snapshot.handoff is None
        # The chat is addressed by its own id only; an archived member's id names no chat.
        assert manager.get_chat_snapshot(first) == snapshot
        assert manager.get_chat_snapshot(second) is None
        active = manager.get_active_agent_info(ChatId(first))
        assert active is not None and active.id == second
        assert manager.get_active_agent_info(ChatId(second)) is None
        assert manager.get_chat_ids() == [ChatId(first)]
    finally:
        manager.stop()


def test_a_recorded_chats_segments_follow_the_record_and_skip_an_agent_mngr_no_longer_lists(
    broadcaster: WebSocketBroadcaster,
) -> None:
    manager, store, first, second = _recorded_chat(broadcaster)
    try:
        segments = manager.get_chat_segments(ChatId(first))
        assert segments is not None
        assert [(s.agent.id, s.seq, s.is_active, s.recorded_event_count) for s in segments] == [
            (first, 1, False, 7),
            (second, 2, True, None),
        ]
        assert manager.get_chat_segments(ChatId(second)) is None

        # An archived member mngr has forgotten leaves a gap; a forgotten active agent leaves no chat.
        manager.remove_agent(first)
        remaining = manager.get_chat_segments(ChatId(first))
        assert remaining is not None and [s.agent.id for s in remaining] == [second]
        manager.remove_agent(second)
        assert manager.get_chat_segments(ChatId(first)) is None
        assert manager.get_chat_snapshots() == []
        # Nothing here destroyed the chat, so its record stands.
        assert store.read(ChatId(first)) is not None
    finally:
        manager.stop()


def test_the_verbs_of_a_recorded_chat_act_on_the_right_agents(
    broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    mngr_binary, argv_log = write_recording_mngr_binary(tmp_path)
    manager, store, first, second = _recorded_chat(broadcaster, mngr_binary)
    try:
        manager.stop_chat(ChatId(first))
        manager.rename_chat(first, "New Name")
        with manager._lock:
            manager._pending_permission_ids_by_agent[second] = {"req-1"}
        assert manager.has_pending_permission(ChatId(first))
        assert not manager.has_pending_permission(ChatId(second))
        # A sign-in restarts the chat's active agent and never its archived member, though
        # both carry an ``account`` label.
        assert manager.restart_agents_on_account("acct-1") == 0
        assert manager.restart_agents_on_account("acct-2") == 1

        manager.destroy_chat(ChatId(first))

        argv_lines = argv_log.read_text().splitlines()
        # The rename is reflected in the tracked name at once, so the restart names the new one.
        assert argv_lines == [
            "stop Chat-1",
            f"rename {second} New-Name --label display_name=New Name",
            "start New-Name --restart --no-resume",
            f"destroy {first} {second} --force",
        ]
        assert store.read(ChatId(first)) is None
        assert manager.get_agent_by_id(first) is None and manager.get_agent_by_id(second) is None
        assert manager.get_chat_snapshots() == []
    finally:
        manager.stop()


def test_stopping_or_destroying_a_recorded_chat_with_no_active_agent_is_refused(
    broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    mngr_binary, argv_log = write_recording_mngr_binary(tmp_path)
    manager, _store, first, second = _recorded_chat(broadcaster, mngr_binary)
    try:
        manager.remove_agent(second)
        with pytest.raises(AgentStopError):
            manager.stop_chat(ChatId(first))
        with pytest.raises(AgentDestroyError):
            manager.destroy_chat(ChatId(first))
        assert not argv_log.exists()
    finally:
        manager.stop()


def test_a_record_that_cannot_be_removed_fails_the_destroy_as_a_destroy_error(
    broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """The agents are gone but the record would resurrect the chat at the next build, so the verb
    reports the failure through the error its callers handle, not a foreign one."""
    mngr_binary, argv_log = write_recording_mngr_binary(tmp_path)
    manager, _store, first, second = _recorded_chat(broadcaster, mngr_binary, store=_UnremovableChatRecordStore())
    try:
        with pytest.raises(AgentDestroyError, match="folder could not be removed"):
            manager.destroy_chat(ChatId(first))
        assert argv_log.read_text().splitlines() == [f"destroy {first} {second} --force"]
    finally:
        manager.stop()


def test_destroying_or_discarding_a_chat_with_no_record_removes_its_folder(
    broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """A chat's fast mode lives in its folder, so a chat that never had a record still has one to remove."""
    mngr_binary, _argv_log = write_recording_mngr_binary(tmp_path)
    chats_root = tmp_path / "chats"
    manager = AgentManager.build(
        broadcaster,
        mngr_binary=mngr_binary,
        chat_record_store=FileChatRecordStore(root=chats_root),
        chat_files_root=chats_root,
    )
    try:
        seed_agent_state(manager, "agent-plain", name="Chat-1")
        manager.set_fast_mode_state(ChatId("agent-plain"), ChatFastModeState(mode=FastModeMode.ON))
        manager.destroy_chat(ChatId("agent-plain"))
        assert not (chats_root / "agent-plain").exists()

        _seed_failed_chat(manager, ChatId("failed-1"), "Chat 2")
        manager.set_fast_mode_state(ChatId("failed-1"), ChatFastModeState(mode=FastModeMode.ON))
        assert manager.discard_provisional_chat("failed-1") is True
        assert not (chats_root / "failed-1").exists()
    finally:
        manager.stop()


def test_an_archived_members_removal_leaves_its_chats_records_and_transcripts_standing(
    broadcaster: WebSocketBroadcaster,
) -> None:
    manager, _store, first, second = _recorded_chat(broadcaster)
    evicted: list[str] = []
    manager.set_watcher_eviction_callback(evicted.append)
    try:
        manager.record_presence(ChatId(first), "client-1", PresenceState.VISIBLE)
        manager.remove_agent(first)
        # The chat's per-chat state (its presence, here) belongs to the chat, not the member.
        assert manager._oom_prioritizer._presence.is_open(ChatId(first))
        assert [snapshot.chat_id for snapshot in manager.get_chat_snapshots()] == [first]
        # The member's own resident transcript goes; the chat's active segment stays.
        assert evicted == [first]

        # The active agent stopping drops the whole chat's resident transcripts, archived
        # segments included, and the observe stream's report of the death is what says so.
        evicted.clear()
        details = _agent_details("Chat-1", agent_id=MngrAgentId(second), state=AgentLifecycleState.RUNNING)
        manager._handle_observe_event(make_agent_state_event(details))
        stopped = details.model_copy_update(to_update(details.field_ref().state, AgentLifecycleState.STOPPED))
        manager._handle_observe_event(make_agent_state_event(stopped))
        assert set(evicted) == {first, second}
    finally:
        manager.stop()


def test_an_archived_member_stopping_evicts_only_its_own_transcript(broadcaster: WebSocketBroadcaster) -> None:
    """A retiring agent that is still running when it is archived stops a moment later; that
    death is the member's, not the chat's, so the active agent's watcher (which a user may be
    viewing) stays resident."""
    manager, _store, first, second = _recorded_chat(broadcaster)
    evicted: list[str] = []
    manager.set_watcher_eviction_callback(evicted.append)
    try:
        for agent_id, name in ((second, "Chat-1"), (first, f"archived-1-Chat-1-{first}")):
            details = _agent_details(name, agent_id=MngrAgentId(agent_id), state=AgentLifecycleState.RUNNING)
            manager._handle_observe_event(make_agent_state_event(details))
        assert evicted == []

        first_details = _agent_details(f"archived-1-Chat-1-{first}", agent_id=MngrAgentId(first))
        stopped = first_details.model_copy_update(
            to_update(first_details.field_ref().state, AgentLifecycleState.STOPPED)
        )
        manager._handle_observe_event(make_agent_state_event(stopped))
        assert evicted == [first]
    finally:
        manager.stop()


def test_removing_an_archived_member_through_the_observe_stream_keeps_the_chat(
    broadcaster: WebSocketBroadcaster,
) -> None:
    manager, _store, first, second = _recorded_chat(broadcaster)
    try:
        manager.record_presence(ChatId(first), "client-1", PresenceState.VISIBLE)
        manager._handle_observe_event(make_agent_state_event(_agent_details("Chat-1", agent_id=MngrAgentId(second))))
        first_details = _agent_details(f"archived-1-Chat-1-{first}", agent_id=MngrAgentId(first))
        manager._handle_observe_event(make_agent_state_event(first_details))
        manager._handle_observe_event(
            make_agent_removed_event(first_details.id, first_details.name, first_details.host.id)
        )
        assert manager._oom_prioritizer._presence.is_open(ChatId(first))
        assert [snapshot.chat_id for snapshot in manager.get_chat_snapshots()] == [first]
    finally:
        manager.stop()


def test_a_recorded_chat_whose_active_agent_is_unknown_lists_nothing(
    broadcaster: WebSocketBroadcaster,
) -> None:
    store = InMemoryChatRecordStore()
    manager = AgentManager.build(broadcaster, chat_record_store=store)
    first, second = f"agent-{uuid4().hex}", f"agent-{uuid4().hex}"
    try:
        seed_agent_state(manager, first, name=f"archived-1-Chat-1-{first}", state="STOPPED")
        store.write(make_two_member_chat_record(first, second))
        manager.refresh_chat_records()
        # The record names the first agent, so it is not a chat of its own either.
        assert manager.get_chat_snapshots() == []
        assert manager.get_chat_snapshot(first) is None
        assert manager.get_chat_ids() == []
    finally:
        manager.stop()


# The handoff: moving a chat to another harness (``chat_handoffs.py`` runs it; these cover the manager's side).


class _TranscriptWithUserTurn(ListTranscriptReader):
    """Three events, the second a user turn the user typed, so a handoff off this agent is not a fresh start."""

    def get_all_events(self, session_id: str | None = None) -> list[dict[str, Any]]:
        events = super().get_all_events(session_id)
        return [
            events[0],
            {**events[1], "type": "user_message", "timestamp": "2026-09-13T11:00:00+00:00"},
            events[2],
        ]


def _handoff_capabilities(
    sent: list[tuple[str, str, str]], is_summary_written: bool = True, has_user_turn: bool = True
) -> HandoffCapabilities:
    """Capabilities whose send records itself and, for the summary request, writes the file at once. The
    retiring agent's transcript carries a user turn unless ``has_user_turn`` is off, which makes every
    handoff a fresh start."""

    def deliver(agent_info: AgentInfo, text: str, message_id: str) -> SendOutcome:
        sent.append((agent_info.id, text, message_id))
        if is_summary_written:
            write_summary_for_request(text)
        return SendOutcome.OK

    event_ids = ["e-1", "e-2", "e-3"]
    return HandoffCapabilities(
        ensure_watcher=lambda agent_info: (
            _TranscriptWithUserTurn(event_ids) if has_user_turn else ListTranscriptReader(event_ids)
        ),
        drain_to_composer=lambda agent_info: "queued text",
        deliver=deliver,
    )


def _openai_account() -> str:
    account_id, _ = mint_account_dir()
    commit_account(account_id, "openai", "OpenAI")
    return account_id


def _handoff_manager(
    broadcaster: WebSocketBroadcaster, tmp_path: Path, sent: list[tuple[str, str, str]], has_user_turn: bool = True
) -> tuple[AgentManager, InMemoryChatRecordStore, Path]:
    mngr_binary, argv_log = write_recording_mngr_binary(tmp_path)
    store = InMemoryChatRecordStore()
    manager = AgentManager.build(
        broadcaster,
        chat_record_store=store,
        mngr_binary=mngr_binary,
        chat_files_root=tmp_path / "chats",
        prompt_template_path=CONTINUE_CHAT_TEMPLATE_PATH,
    )
    manager.set_handoff_capabilities(_handoff_capabilities(sent, has_user_turn=has_user_turn))
    return manager, store, argv_log


def _wait_until_settled(store: InMemoryChatRecordStore, chat_id: ChatId) -> ChatRecord:
    wait_for(lambda: (record := store.read(chat_id)) is not None and record.handoff is None, timeout=15.0)
    record = store.read(chat_id)
    assert record is not None
    return record


def test_a_handoff_moves_a_chat_to_a_new_agent_on_another_harness(
    broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    sent: list[tuple[str, str, str]] = []
    manager, store, argv_log = _handoff_manager(broadcaster, tmp_path, sent)
    first = f"agent-{uuid4().hex}"
    seed_agent_state(manager, first, name="Chat-1", labels={"display_name": "Chat 1", "account": "acct-anthropic"})
    account = _openai_account()
    try:
        phase, block = manager.begin_handoff(ChatId(first), account, "Carry on in Codex", "m-1", HeldSendOrigin.CLIENT)
        assert (phase, block) == (HandoffPhase.SUMMARIZING, "queued text")

        record = _wait_until_settled(store, ChatId(first))
        assert [entry.harness for entry in record.agents] == [HarnessType.CLAUDE, HarnessType.CODEX]
        successor = record.agents[1].agent_id
        assert record.agents[0].archived_name == f"archived-1-Chat-1-{first}"
        assert record.agents[0].final_event_count == 3
        assert record.agents[1].account_id == account and record.agents[1].lane == "openai"

        # The chat lists once, from its new agent, under its old title and name.
        (snapshot,) = manager.get_chat_snapshots()
        assert (snapshot.chat_id, snapshot.agent_ids) == (first, (first, successor))
        assert snapshot.active_agent.agent_id == successor and snapshot.active_agent.harness is HarnessType.CODEX
        assert (snapshot.title, snapshot.name, snapshot.handoff) == ("Chat 1", "Chat-1", None)
        archived = manager.get_agent_by_id(first)
        assert archived is not None and archived.state == "STOPPED"
        assert archived.labels["chat_seq"] == "1" and archived.labels["chat_id"] == first

        verbs = [line.split(" ")[0] for line in argv_log.read_text().splitlines()]
        assert verbs == ["stop", "rename", "create"]
        assert sent[0][0] == first and sent[0][1].startswith("/handoff-summary ")
        # The prompt reached the successor through the send path, with the message at its end.
        assert sent[1][0] == successor and sent[1][2].startswith("handoff-prompt-")
        assert sent[1][1].endswith("Carry on in Codex\n")
        assert "--message" not in argv_log.read_text()
    finally:
        manager.stop()


def test_a_handoff_off_an_agent_with_no_user_turn_is_a_fresh_start(
    broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """Nothing to summarize: no request goes out, no prompt is written, and the confirming message (or nothing,
    for a switch made from the provider menu) reaches the successor as an ordinary send."""
    sent: list[tuple[str, str, str]] = []
    manager, store, argv_log = _handoff_manager(broadcaster, tmp_path, sent, has_user_turn=False)
    first = f"agent-{uuid4().hex}"
    seed_agent_state(manager, first, name="Chat-1", labels={"display_name": "Chat 1", "account": "acct-anthropic"})
    try:
        phase, _block = manager.begin_handoff(ChatId(first), _openai_account(), "", "m-1", HeldSendOrigin.CLIENT)
        assert phase is HandoffPhase.SUMMARIZING
        record = _wait_until_settled(store, ChatId(first))
        assert [entry.harness for entry in record.agents] == [HarnessType.CLAUDE, HarnessType.CODEX]
        assert [entry.is_fresh_start for entry in record.agents] == [False, True]
        assert sent == []
        assert not (tmp_path / "chats" / first / "summaries").exists()
        assert [line.split(" ")[0] for line in argv_log.read_text().splitlines()] == ["stop", "rename", "create"]
        # The snapshot never listed a confirming message, since none was typed.
        assert manager.get_handoff_state(ChatId(first)) is None
    finally:
        manager.stop()


def test_a_model_pick_the_successor_cannot_take_fails_the_switch_at_the_model_step_and_a_retry_adopts_it(
    broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """The recording mngr's successor has no daemon and no sidecar, so its option set is empty and the pick is
    unknown: the switch fails after the create, the page names the pick, a retry on the same account runs no
    second create, and a retry on another account destroys the successor and creates afresh."""
    sent: list[tuple[str, str, str]] = []
    manager, store, argv_log = _handoff_manager(broadcaster, tmp_path, sent)
    evicted: list[str] = []
    manager.set_watcher_eviction_callback(evicted.append)
    first = f"agent-{uuid4().hex}"
    seed_agent_state(manager, first, name="Chat-1", labels={"display_name": "Chat 1", "account": "acct-anthropic"})
    chat_id = ChatId(first)
    pick = ModelPick(model_id="gpt-6-astra", effort="high")
    try:
        manager.begin_handoff(chat_id, _openai_account(), "Carry on in Codex", "m-1", HeldSendOrigin.CLIENT, pick)

        def is_failed() -> bool:
            record = store.read(chat_id)
            return record is not None and record.handoff is not None and record.handoff.phase is HandoffPhase.FAILED

        wait_for(is_failed, timeout=15.0)
        record = store.read(chat_id)
        assert record is not None and record.handoff is not None
        assert record.handoff.failed_step is HandoffFailedStep.MODEL
        assert record.handoff.error == "Unknown model 'gpt-6-astra'"
        assert record.handoff.model_pick == pick
        successor = record.handoff.next_agent_id
        # The successor exists and is tracked, hidden inside its chat rather than listed as one of its own.
        assert manager.get_agent_by_id(successor) is not None
        (snapshot,) = manager.get_chat_snapshots()
        assert snapshot.status is InstanceStatus.ERROR and snapshot.handoff is not None
        assert snapshot.handoff.failed_step is HandoffFailedStep.MODEL
        assert [line.split(" ")[0] for line in argv_log.read_text().splitlines()] == ["stop", "rename", "create"]
        # Only the summary request went out: the prompt waits for the pick.
        assert [message_id for _agent, _text, message_id in sent] == [f"handoff-summary-{record.handoff.handoff_id}"]

        # A retry on the same account adopts the successor: no create, the pick fails again the same way.
        assert manager.retry_handoff(chat_id, record.handoff.target_account_id) is HandoffPhase.SWITCHING
        wait_for(is_failed, timeout=15.0)
        assert [line.split(" ")[0] for line in argv_log.read_text().splitlines()] == ["stop", "rename", "create"]

        # A retry on another account of the same harness keeps the pick, destroys the successor, and
        # creates afresh under the same pre-minted id.
        assert manager.retry_handoff(chat_id, _openai_account()) is HandoffPhase.SWITCHING
        wait_for(is_failed, timeout=15.0)
        verbs = [line.split(" ")[0] for line in argv_log.read_text().splitlines()]
        assert verbs == ["stop", "rename", "create", "destroy", "create"]
        assert f"destroy {successor} --force" in argv_log.read_text().splitlines()
        assert argv_log.read_text().splitlines()[-1].split(" ")[3] == successor
        retried = store.read(chat_id)
        assert retried is not None and retried.handoff is not None and retried.handoff.model_pick == pick
        # Discarding it forgot it the way a destroy does, trackers and resident transcript included,
        # rather than only dropping it from the tracked list.
        assert evicted == [successor]
    finally:
        manager.stop()


def test_a_switch_that_failed_after_its_successor_was_adopted_retries_only_where_the_chat_moved(
    broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """A delivery refused past the adoption leaves the successor as the chat's own agent, with only the
    deliveries left to do. A retry naming another account is refused: destroying that agent to create
    another under the same id would take the chat's agent away and leave the create step skipped (its
    guard reads the record's last entry), so the chat would list nothing at all."""
    sent: list[tuple[str, str, str]] = []

    def deliver(agent_info: AgentInfo, text: str, message_id: str) -> SendOutcome:
        # The prompt goes out after the successor is on the record; failing it is how a real
        # refusal past the point of no return lands (a record write, ensure_watcher, a held send).
        if message_id.startswith("handoff-prompt-"):
            raise OSError("the successor's pane went away")
        sent.append((agent_info.id, text, message_id))
        write_summary_for_request(text)
        return SendOutcome.OK

    mngr_binary, argv_log = write_recording_mngr_binary(tmp_path)
    store = InMemoryChatRecordStore()
    manager = AgentManager.build(
        broadcaster,
        chat_record_store=store,
        mngr_binary=mngr_binary,
        chat_files_root=tmp_path / "chats",
        prompt_template_path=CONTINUE_CHAT_TEMPLATE_PATH,
    )
    manager.set_handoff_capabilities(
        HandoffCapabilities(
            ensure_watcher=lambda agent_info: _TranscriptWithUserTurn(["e-1", "e-2", "e-3"]),
            drain_to_composer=lambda agent_info: "queued text",
            deliver=deliver,
        )
    )
    first = f"agent-{uuid4().hex}"
    seed_agent_state(manager, first, name="Chat-1", labels={"display_name": "Chat 1", "account": "acct-anthropic"})
    chat_id = ChatId(first)
    try:
        manager.begin_handoff(chat_id, _openai_account(), "Carry on in Codex", "m-1", HeldSendOrigin.CLIENT)

        def is_failed() -> bool:
            record = store.read(chat_id)
            return record is not None and record.handoff is not None and record.handoff.phase is HandoffPhase.FAILED

        wait_for(is_failed, timeout=15.0)
        record = store.read(chat_id)
        assert record is not None and record.handoff is not None
        successor = record.handoff.next_agent_id
        assert record.agents[-1].agent_id == successor

        with pytest.raises(HandoffError):
            manager.retry_handoff(chat_id, _openai_account())

        # Refused before anything was written or destroyed: the chat still runs on its successor.
        assert "destroy" not in argv_log.read_text()
        refused = store.read(chat_id)
        assert refused is not None and refused.handoff is not None
        assert refused.handoff.phase is HandoffPhase.FAILED
        assert refused.handoff.next_agent_id == successor
        (snapshot,) = manager.get_chat_snapshots()
        assert snapshot.active_agent.agent_id == successor
    finally:
        manager.stop()


def test_a_pick_is_refused_for_a_switch_that_keeps_the_agent(
    broadcaster: WebSocketBroadcaster, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rebind keeps the agent's model settings, so a pick beside it is a refusal, not a silent drop."""
    sent: list[tuple[str, str, str]] = []
    manager, _store, _argv_log, agent_id, _first_account, second_account = _rebind_manager(
        broadcaster, tmp_path, monkeypatch, sent
    )
    try:
        with pytest.raises(HandoffError, match="keeps its model settings"):
            manager.begin_switch(
                ChatId(agent_id),
                second_account,
                "hi",
                "m-1",
                HeldSendOrigin.CLIENT,
                model_pick=ModelPick(model_id="opus"),
            )
        assert manager.get_handoff_state(ChatId(agent_id)) is None
    finally:
        manager.stop()


def _converging_chat(
    manager: AgentManager,
    store: InMemoryChatRecordStore,
    *,
    phase: HandoffPhase,
    is_retiring_archived: bool,
    target_account_id: str = "acct-openai",
) -> tuple[str, str]:
    """A one-agent chat whose record carries a handoff in ``phase``; returns the agent id and the successor's."""
    first, successor = f"agent-{uuid4().hex}", f"agent-{uuid4().hex}"
    entry = make_chat_agent_entry(1, first, is_archived=is_retiring_archived)
    seed_agent_state(
        manager,
        first,
        name=entry.archived_name if is_retiring_archived and entry.archived_name is not None else "Chat-1",
        state="STOPPED" if is_retiring_archived else "RUNNING",
        labels={"display_name": "Chat 1", "account": "acct-anthropic"},
    )
    handoff = make_chat_handoff_record(
        retiring_seq=1, next_agent_id=successor, phase=phase, target_account_id=target_account_id
    )
    if phase in (HandoffPhase.SWITCHING, HandoffPhase.FAILED):
        handoff = handoff.model_copy_update(
            to_update(handoff.field_ref().held_sends, ()),
            to_update(handoff.field_ref().prompt, "the stored prompt"),
            to_update(handoff.field_ref().summary_outcome, SummaryOutcome.WRITTEN),
            to_update(
                handoff.field_ref().error, "mngr create exited with code 3" if phase is HandoffPhase.FAILED else None
            ),
        )
    store.write(ChatRecord(chat_id=ChatId(first), agents=(entry,), handoff=handoff))
    manager.refresh_chat_records()
    return first, successor


def test_a_converging_chat_holds_sends_refuses_the_verbs_and_can_be_cancelled(
    broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    sent: list[tuple[str, str, str]] = []
    manager, store, argv_log = _handoff_manager(broadcaster, tmp_path, sent)
    first, _successor = _converging_chat(manager, store, phase=HandoffPhase.SUMMARIZING, is_retiring_archived=False)
    chat_id = ChatId(first)
    try:
        assert manager.hold_send(chat_id, "m-2", "and this", HeldSendOrigin.SCRIPT) is HandoffPhase.SUMMARIZING
        # A retried send is not held twice; a chat that is not converging is not held at all.
        assert manager.hold_send(chat_id, "m-2", "and this", HeldSendOrigin.SCRIPT) is HandoffPhase.SUMMARIZING
        assert manager.hold_send(ChatId(f"agent-{uuid4().hex}"), "m-3", "x", HeldSendOrigin.SCRIPT) is None
        record = store.read(chat_id)
        assert record is not None and record.handoff is not None
        assert [held.message_id for held in record.handoff.held_sends] == ["trigger-1", "m-2"]

        (snapshot,) = manager.get_chat_snapshots()
        assert snapshot.status is InstanceStatus.WORKING
        assert snapshot.handoff is not None and snapshot.handoff.phase is HandoffPhase.SUMMARIZING
        assert manager.get_handoff_state(chat_id) == snapshot.handoff

        with pytest.raises(ChatConvergingError):
            manager.stop_chat(chat_id)
        with pytest.raises(ChatConvergingError):
            manager.rename_chat(first, "Other")
        with pytest.raises(ChatConvergingError):
            manager.begin_handoff(chat_id, _openai_account(), "again", "m-4", HeldSendOrigin.CLIENT)

        # Cancel: the trigger comes back, the other held send goes to the agent the chat stays on, and a
        # first-handoff record disappears so the chat is its one agent again.
        assert manager.cancel_handoff(chat_id) == "Carry on in Codex"
        assert store.read(chat_id) is None
        wait_for(lambda: (first, "and this", "m-2") in sent, timeout=5.0)
        assert manager.get_chat_snapshot(first) is not None and manager.get_handoff_state(chat_id) is None
        with pytest.raises(HandoffError):
            manager.cancel_handoff(chat_id)
        assert not argv_log.exists()
    finally:
        manager.stop()


def test_a_failed_handoff_lists_as_an_error_and_a_retry_creates_the_successor(
    broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    sent: list[tuple[str, str, str]] = []
    manager, store, argv_log = _handoff_manager(broadcaster, tmp_path, sent)
    first, successor = _converging_chat(manager, store, phase=HandoffPhase.FAILED, is_retiring_archived=True)
    chat_id = ChatId(first)
    try:
        (snapshot,) = manager.get_chat_snapshots()
        assert snapshot.status is InstanceStatus.ERROR
        assert snapshot.handoff is not None and snapshot.handoff.error == "mngr create exited with code 3"
        # The stand-in already carries its archival name; the snapshot still says what the chat is called.
        assert (snapshot.title, snapshot.name) == ("Chat 1", "Chat-1")
        with pytest.raises(ChatConvergingError, match="switch to Codex can no longer be called off"):
            manager.cancel_handoff(chat_id)
        # The trigger already rides the stored prompt: a retried send with its id is not held again.
        assert (
            manager.hold_send(chat_id, "trigger-1", "Carry on in Codex", HeldSendOrigin.CLIENT) is HandoffPhase.FAILED
        )
        failed = store.read(chat_id)
        assert failed is not None and failed.handoff is not None and failed.handoff.held_sends == ()

        assert manager.retry_handoff(chat_id, _openai_account()) is HandoffPhase.SWITCHING
        record = _wait_until_settled(store, chat_id)
        assert [entry.agent_id for entry in record.agents] == [first, successor]
        verbs = [line.split(" ")[0] for line in argv_log.read_text().splitlines()]
        assert verbs == ["create"]
        assert f"--id {successor}" in argv_log.read_text()
        assert manager.get_chat_snapshot(first) is not None
        with pytest.raises(HandoffError):
            manager.retry_handoff(chat_id, _openai_account())
    finally:
        manager.stop()


def test_destroying_one_agent_runs_the_shared_destroy_and_reports_a_refusal(
    broadcaster: WebSocketBroadcaster, tmp_path: Path, false_binary: str
) -> None:
    """The handoff's destroy of a half-made successor is the chat destroy's own ``mngr destroy --force``, by id."""
    mngr_binary, argv_log = write_recording_mngr_binary(tmp_path)
    manager = AgentManager.build(broadcaster, mngr_binary=mngr_binary)
    refusing = AgentManager.build(broadcaster, mngr_binary=false_binary)
    agent_id = f"agent-{uuid4().hex}"
    try:
        manager.destroy_agent_process(agent_id)
        assert argv_log.read_text().splitlines() == [f"destroy {agent_id} --force"]
        with pytest.raises(AgentDestroyError, match=f"Failed to destroy agent '{agent_id}'"):
            refusing.destroy_agent_process(agent_id)
    finally:
        manager.stop()
        refusing.stop()


def test_an_unfinished_handoff_resumes_when_the_manager_starts(
    broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    sent: list[tuple[str, str, str]] = []
    manager, store, argv_log = _handoff_manager(broadcaster, tmp_path, sent)
    first, successor = _converging_chat(
        manager, store, phase=HandoffPhase.SWITCHING, is_retiring_archived=True, target_account_id=_openai_account()
    )
    try:
        manager._resume_handoffs()
        record = _wait_until_settled(store, ChatId(first))
        assert [entry.agent_id for entry in record.agents] == [first, successor]
        assert [line.split(" ")[0] for line in argv_log.read_text().splitlines()] == ["create"]
    finally:
        manager.stop()


def test_a_successor_being_made_is_its_chats_and_not_a_chat_of_its_own(
    broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """The observe stream tracks the successor as soon as mngr lists it, before the record appends it;
    the chat keeps listing once, from the retiring stand-in, the successor resolves to that chat, and
    destroying the chat takes the successor with it."""
    sent: list[tuple[str, str, str]] = []
    manager, store, argv_log = _handoff_manager(broadcaster, tmp_path, sent)
    first, successor = _converging_chat(manager, store, phase=HandoffPhase.SWITCHING, is_retiring_archived=True)
    seed_agent_state(
        manager,
        successor,
        name="Chat-1",
        harness=HarnessType.CODEX,
        labels={"display_name": "Chat 1", "chat_id": first, "chat_seq": "2"},
    )
    try:
        (snapshot,) = manager.get_chat_snapshots()
        assert (snapshot.chat_id, snapshot.active_agent.agent_id, snapshot.status) == (
            first,
            first,
            InstanceStatus.WORKING,
        )
        assert manager.get_chat_snapshot(successor) is None
        assert manager.get_active_agent_info(ChatId(successor)) is None
        assert manager.chat_id_of_agent(successor) == ChatId(first)

        manager.destroy_chat(ChatId(first))
        assert argv_log.read_text().splitlines() == [f"destroy {first} {successor} --force"]
        assert manager.get_agent_by_id(successor) is None and store.read(ChatId(first)) is None
    finally:
        manager.stop()


def test_a_handoff_is_refused_for_the_wrong_targets(broadcaster: WebSocketBroadcaster, tmp_path: Path) -> None:
    sent: list[tuple[str, str, str]] = []
    manager, _store, _argv_log = _handoff_manager(broadcaster, tmp_path, sent)
    first = f"agent-{uuid4().hex}"
    anthropic_id, _ = mint_account_dir()
    commit_account(anthropic_id, "anthropic", "Anthropic")
    seed_agent_state(manager, first, name="Chat-1", labels={"display_name": "Chat 1", "account": anthropic_id})
    try:
        with pytest.raises(HandoffError, match="already runs on account"):
            manager.begin_handoff(ChatId(first), anthropic_id, "hi", "m-1", HeldSendOrigin.CLIENT)
        with pytest.raises(HandoffError):
            manager.begin_handoff(ChatId(first), "acct-missing", "hi", "m-1", HeldSendOrigin.CLIENT)
        # A row naming a lane this build no longer has is refused by name, not as "no such account".
        retired_lane_id, _ = mint_account_dir()
        commit_account(retired_lane_id, "lane-this-build-lacks", "Elsewhere")
        with pytest.raises(HandoffError, match="lane this build does not have"):
            manager.begin_switch(ChatId(first), retired_lane_id, "hi", "m-1", HeldSendOrigin.CLIENT)
        with pytest.raises(HandoffError, match="no active agent"):
            manager.begin_handoff(
                ChatId(f"agent-{uuid4().hex}"), _openai_account(), "hi", "m-1", HeldSendOrigin.CLIENT
            )
        assert sent == []
    finally:
        manager.stop()


# The rebind: changing a chat's account in place (``chat_rebinds.py`` runs it; these cover the manager's side).


def _anthropic_account(display: str = "Anthropic") -> str:
    account_id, _ = mint_account_dir()
    commit_account(account_id, "anthropic", display)
    return account_id


def _rebind_manager(
    broadcaster: WebSocketBroadcaster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sent: list[tuple[str, str, str]],
) -> tuple[AgentManager, InMemoryChatRecordStore, Path, str, str, str]:
    """A manager over the test's own host dir, tracking a claude chat bound to one Anthropic account with a second
    signed in; returns it with the record store, the mngr argv log, the agent id, and the two account ids."""
    host_dir = tmp_path / "host"
    monkeypatch.setenv("MNGR_HOST_DIR", str(host_dir))
    manager, store, argv_log = _handoff_manager(broadcaster, tmp_path, sent)
    first_account, second_account = _anthropic_account(), _anthropic_account()
    agent_id = f"agent-{uuid4().hex}"
    seed_agent_state(manager, agent_id, name="Chat-1", labels={"display_name": "Chat 1", "account": first_account})
    state_dir = host_dir / "agents" / agent_id
    state_dir.mkdir(parents=True)
    (state_dir / "env").write_text(f"MNGR_AGENT_ID={agent_id}\nCLAUDE_CONFIG_DIR={account_dir(first_account)}\n")
    (state_dir / "claude_session_id_history").write_text("session-one\n")
    project_dir = account_dir(first_account) / "projects" / "-home-user-workspace"
    project_dir.mkdir(parents=True)
    (project_dir / "session-one.jsonl").write_text('{"type":"user"}\n')
    return manager, store, argv_log, agent_id, first_account, second_account


def test_a_switch_to_an_account_on_the_chats_own_lane_rebinds_the_agent_in_place(
    broadcaster: WebSocketBroadcaster, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[tuple[str, str, str]] = []
    manager, store, argv_log, agent_id, first_account, second_account = _rebind_manager(
        broadcaster, tmp_path, monkeypatch, sent
    )
    chat_id = ChatId(agent_id)
    try:
        kind, phase, block = manager.begin_switch(
            chat_id, second_account, "Carry on here", "m-1", HeldSendOrigin.CLIENT
        )
        assert (kind, phase, block) == (TransitionKind.REBIND, HandoffPhase.RESTARTING, "queued text")
        wait_for(lambda: manager.get_handoff_state(chat_id) is None, timeout=15.0)

        # stop, relabel, start: the agent stayed the chat's, on the new account.
        argv = argv_log.read_text().splitlines()
        assert argv == [
            "stop Chat-1",
            f"label {agent_id} --label account={second_account}",
            "start Chat-1 --no-resume",
        ]
        snapshot = manager.get_chat_snapshot(agent_id)
        assert snapshot is not None and snapshot.handoff is None
        assert snapshot.active_agent.account_id == second_account and snapshot.agent_ids == (agent_id,)
        assert snapshot.active_agent.state == "WAITING"
        # The env line and the session files followed the agent to the new account's folder.
        env_text = (tmp_path / "host" / "agents" / agent_id / "env").read_text()
        assert env_text == f"MNGR_AGENT_ID={agent_id}\nCLAUDE_CONFIG_DIR={account_dir(second_account)}\n"
        assert (account_dir(second_account) / "projects" / "-home-user-workspace" / "session-one.jsonl").exists()
        # The confirming message went through the ordinary send path once the agent was back; the
        # one-agent record is gone again, and the target became the most recently used account.
        assert sent == [(agent_id, "Carry on here", "m-1")]
        assert store.read(chat_id) is None
        assert read_index().mru == second_account
    finally:
        manager.stop()


def test_the_switch_target_rule_keeps_the_agent_only_for_its_own_harness_and_lane() -> None:
    claude = AgentStateItem(id="agent-1", name="c", state="RUNNING", labels={"account": "acct-a"}, work_dir=None)
    anthropic_2 = Account(id="acct-a2", lane="anthropic", seq=2, display="Anthropic")
    openrouter = Account(id="acct-r", lane="openrouter", seq=1, display="OpenRouter")
    opencode_go = Account(id="acct-g", lane="opencode-go", seq=1, display="Opencode Go")
    first_anthropic, _ = mint_account_dir()
    commit_account(first_anthropic, "anthropic", "Anthropic")
    first_openrouter, _ = mint_account_dir()
    commit_account(first_openrouter, "openrouter", "OpenRouter")
    bound_claude = claude.model_copy_update(to_update(claude.field_ref().labels, {"account": first_anthropic}))
    pi = AgentStateItem(
        id="agent-2",
        name="p",
        state="RUNNING",
        labels={"account": first_openrouter},
        work_dir=None,
        harness=HarnessType.PI_CODING,
    )

    def target(account: Account, harness: HarnessType) -> _SwitchTarget:
        return _SwitchTarget(account=account, harness=harness, label=account.display)

    assert is_rebind_target(bound_claude, target(anthropic_2, HarnessType.CLAUDE)) is True
    assert is_rebind_target(bound_claude, target(openrouter, HarnessType.PI_CODING)) is False
    # Two lanes on one harness: a move between them replaces the agent.
    assert is_rebind_target(pi, target(opencode_go, HarnessType.PI_CODING)) is False
    assert (
        is_rebind_target(
            pi,
            target(
                openrouter.model_copy_update(to_update(openrouter.field_ref().id, "acct-r2")), HarnessType.PI_CODING
            ),
        )
        is True
    )
    # An agent whose account label names a deleted account has no lane to match.
    assert is_rebind_target(claude, target(anthropic_2, HarnessType.CLAUDE)) is False
    unscoped = AgentStateItem(
        id="agent-3", name="o", state="RUNNING", labels={}, work_dir=None, harness=HarnessType.OPENCODE
    )
    assert is_rebind_target(unscoped, target(anthropic_2, HarnessType.OPENCODE)) is False


def test_a_retry_on_an_unwired_manager_leaves_the_failed_phase_as_it_is(
    broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """The refusal comes before anything is written: the page keeps its failed notice and its retry."""
    mngr_binary, _argv_log = write_recording_mngr_binary(tmp_path)
    store = InMemoryChatRecordStore()
    manager = AgentManager.build(broadcaster, chat_record_store=store, mngr_binary=mngr_binary)
    first_account, second_account = _anthropic_account(), _anthropic_account()
    agent_id = f"agent-{uuid4().hex}"
    chat_id = ChatId(agent_id)
    seed_agent_state(manager, agent_id, name="Chat-1", labels={"display_name": "Chat 1", "account": first_account})
    failed = make_chat_rebind_record(agent_id=agent_id, phase=HandoffPhase.FAILED, target_account_id=second_account)
    store.write(
        ChatRecord(
            chat_id=chat_id,
            agents=(make_chat_agent_entry(1, agent_id, is_archived=False, account_id=first_account),),
            rebind=failed,
        )
    )
    manager.refresh_chat_records()
    try:
        with pytest.raises(HandoffError, match="not wired"):
            manager.retry_handoff(chat_id, second_account)
        assert store.read(chat_id) == ChatRecord(
            chat_id=chat_id,
            agents=(make_chat_agent_entry(1, agent_id, is_archived=False, account_id=first_account),),
            rebind=failed,
        )
    finally:
        manager.stop()


def test_a_rebind_cannot_be_cancelled_holds_sends_and_retries_only_on_its_own_lane(
    broadcaster: WebSocketBroadcaster, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[tuple[str, str, str]] = []
    manager, store, argv_log, agent_id, first_account, second_account = _rebind_manager(
        broadcaster, tmp_path, monkeypatch, sent
    )
    chat_id = ChatId(agent_id)
    rebind = make_chat_rebind_record(
        agent_id=agent_id,
        phase=HandoffPhase.FAILED,
        target_account_id=second_account,
        error="mngr start exited with code 1",
    )
    try:
        store.write(
            ChatRecord(
                chat_id=chat_id,
                agents=(make_chat_agent_entry(1, agent_id, is_archived=False, account_id=first_account),),
                rebind=rebind,
            )
        )
        manager.refresh_chat_records()
        state = manager.get_handoff_state(chat_id)
        assert state is not None and state.kind is TransitionKind.REBIND and state.phase is HandoffPhase.FAILED
        assert state.target_label == "Anthropic 2 (Claude Code)"
        assert state.started_at == rebind.started_at
        snapshot = manager.get_chat_snapshot(agent_id)
        assert snapshot is not None and snapshot.status.value == "error"

        with pytest.raises(ChatConvergingError, match="cannot be called off"):
            manager.cancel_handoff(chat_id)
        assert manager.hold_send(chat_id, "m-2", "and this", HeldSendOrigin.CLIENT) is HandoffPhase.FAILED
        with pytest.raises(ChatConvergingError, match="switch to Anthropic 2 \\(Claude Code\\) failed; retry"):
            manager.stop_chat(chat_id)
        with pytest.raises(HandoffError, match="same harness and lane"):
            manager.retry_handoff(chat_id, _openai_account())

        assert manager.retry_handoff(chat_id, second_account) is HandoffPhase.RESTARTING
        wait_for(lambda: manager.get_handoff_state(chat_id) is None, timeout=15.0)
        # The agent is seeded running, so the retry stops it before relabelling and starting; both
        # held sends follow once it is back.
        assert [line.split(" ")[0] for line in argv_log.read_text().splitlines()] == ["stop", "label", "start"]
        assert sent == [(agent_id, "Carry on on the other account", "trigger-1"), (agent_id, "and this", "m-2")]
        assert store.read(chat_id) is None
    finally:
        manager.stop()


def test_a_failed_rebind_retries_on_its_lane_even_after_the_failed_target_was_signed_out(
    broadcaster: WebSocketBroadcaster, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The relabel runs before the start, so the agent's label names the failed target; deleting that account must
    not stop a retry on another account of the lane."""
    sent: list[tuple[str, str, str]] = []
    manager, store, argv_log, agent_id, first_account, second_account = _rebind_manager(
        broadcaster, tmp_path, monkeypatch, sent
    )
    chat_id = ChatId(agent_id)
    third_account = _anthropic_account()
    try:
        seed_agent_state(
            manager, agent_id, name="Chat-1", labels={"display_name": "Chat 1", "account": second_account}
        )
        store.write(
            ChatRecord(
                chat_id=chat_id,
                agents=(make_chat_agent_entry(1, agent_id, is_archived=False, account_id=first_account),),
                rebind=make_chat_rebind_record(
                    agent_id=agent_id,
                    phase=HandoffPhase.FAILED,
                    target_account_id=second_account,
                    error="mngr start exited with code 1",
                ),
            )
        )
        manager.refresh_chat_records()
        delete_account(second_account)

        assert manager.retry_handoff(chat_id, third_account) is HandoffPhase.RESTARTING
        wait_for(lambda: manager.get_handoff_state(chat_id) is None, timeout=15.0)
        assert f"label {agent_id} --label account={third_account}" in argv_log.read_text().splitlines()
        snapshot = manager.get_chat_snapshot(agent_id)
        assert snapshot is not None and snapshot.active_agent.account_id == third_account
        assert sent == [(agent_id, "Carry on on the other account", "trigger-1")]
    finally:
        manager.stop()


def test_the_rebind_runners_record_callbacks_raise_its_own_cancelled_error(
    broadcaster: WebSocketBroadcaster, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner catches only ``RebindCancelledError`` as the quiet stop, so a record that no longer carries the
    rebind must reach it as that, from the held-send pop as much as from the record update."""
    sent: list[tuple[str, str, str]] = []
    manager, _store, _argv_log, agent_id, _first_account, _second_account = _rebind_manager(
        broadcaster, tmp_path, monkeypatch, sent
    )
    try:
        deps = manager._rebind_runner()._deps
        with pytest.raises(RebindCancelledError):
            deps.take_next_held_send(ChatId(agent_id), "rebind-gone")
        with pytest.raises(RebindCancelledError):
            deps.update_record(ChatId(agent_id), "rebind-gone", lambda record: record)
    finally:
        manager.stop()
