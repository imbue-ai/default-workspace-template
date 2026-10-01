"""The harness-switch coordinator: what it refuses, and what it does when it breaks.

Every refusal is checked on the REQUEST thread, so these tests are the contract the
user actually experiences -- a click that cannot work is answered immediately, with a
status that says whether trying again later would help. The accepted path is exercised
end to end against real harnesses; what is pinned here is that a failure before the
commit point leaves the chat exactly as it was.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from imbue.system_interface.accounts import commit_account
from imbue.system_interface.accounts import mint_account_dir
from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.agent_manager import AgentManager
from imbue.system_interface.chat_registry import ChatRegistry
from imbue.system_interface.chat_registry import chats_dir_for_layout_dir
from imbue.system_interface.handoff_archive import TranscriptArchive
from imbue.system_interface.harness_handoff import HandoffCoordinator
from imbue.system_interface.harness_handoff import HandoffError
from imbue.system_interface.harnesses.harness_type import HarnessType
from imbue.system_interface.harnesses.session_watcher import AgentSessionWatcher
from imbue.system_interface.models import AgentStateItem
from imbue.system_interface.models import ChatId
from imbue.system_interface.models import HandoffPhase
from imbue.system_interface.models import QueuedMessageState
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster

_CHAT_ID = ChatId("agent-" + "a" * 32)


def _unused_watcher(agent_info: AgentInfo) -> AgentSessionWatcher:
    raise AssertionError(f"a refused switch must not open a watcher (asked for {agent_info.id})")


def _unused_evict(agent_id: str) -> None:
    raise AssertionError(f"a refused switch must not evict a watcher (asked for {agent_id})")


@pytest.fixture()
def codex_account_id() -> str:
    account_id, _ = mint_account_dir()
    commit_account(account_id, "openai", "OpenAI")
    return account_id


def _build_manager(tmp_path: Path, mngr_binary: str = "mngr") -> AgentManager:
    return AgentManager.build(
        WebSocketBroadcaster(),
        mngr_binary=mngr_binary,
        chat_registry=ChatRegistry(chats_dir=chats_dir_for_layout_dir(tmp_path)),
    )


def _seed_idle_claude_chat(manager: AgentManager, **overrides: object) -> None:
    """One recorded chat, on claude, idle, with nothing queued: the switchable case."""
    fields: dict[str, object] = {
        "id": str(_CHAT_ID),
        "name": "Chat-1",
        "state": "RUNNING",
        "labels": {"user_created": "true", "display_name": "Chat 1"},
        "work_dir": None,
        "harness": HarnessType.CLAUDE,
        "activity_state": ActivityState.IDLE,
    }
    fields.update(overrides)
    with manager._lock:
        manager._agents[str(_CHAT_ID)] = AgentStateItem.model_validate(fields)
    manager.chat_registry.ensure_chat(_CHAT_ID, str(_CHAT_ID), HarnessType.CLAUDE, None)


@pytest.fixture()
def coordinator(tmp_path: Path) -> Iterator[HandoffCoordinator]:
    manager = _build_manager(tmp_path)
    built = HandoffCoordinator(
        agent_manager=manager,
        transcript_archive=TranscriptArchive(archives_dir=None),
        get_watcher=_unused_watcher,
        evict_watcher=_unused_evict,
    )
    yield built
    built.close()
    manager.stop()


def test_switching_an_unrecorded_chat_is_a_404(coordinator: HandoffCoordinator, codex_account_id: str) -> None:
    """404 rather than a bootstrap: a chat with no record resolves by identity, and
    re-pointing something that resolves by identity would silently do nothing."""
    with pytest.raises(HandoffError) as caught:
        coordinator.start_switch(_CHAT_ID, codex_account_id, "op-1")

    assert caught.value.http_status == 404


def test_switching_a_chat_whose_agent_is_gone_is_a_409(coordinator: HandoffCoordinator, codex_account_id: str) -> None:
    """A record with no live agent: nothing to freeze, snapshot, or retire."""
    coordinator.agent_manager.chat_registry.ensure_chat(_CHAT_ID, str(_CHAT_ID), HarnessType.CLAUDE, None)

    with pytest.raises(HandoffError) as caught:
        coordinator.start_switch(_CHAT_ID, codex_account_id, "op-1")

    assert caught.value.http_status == 409
    assert "not running" in str(caught.value)


def test_switching_onto_the_harness_the_chat_already_runs_is_a_409(coordinator: HandoffCoordinator) -> None:
    """Refused rather than treated as a no-op: it would destroy and rebuild the agent,
    losing the live session to accomplish nothing."""
    account_id, _ = mint_account_dir()
    commit_account(account_id, "anthropic", "Anthropic")
    _seed_idle_claude_chat(coordinator.agent_manager)

    with pytest.raises(HandoffError) as caught:
        coordinator.start_switch(_CHAT_ID, account_id, "op-1")

    assert caught.value.http_status == 409
    assert "already runs on" in str(caught.value)


def test_switching_a_mid_turn_chat_is_a_409(coordinator: HandoffCoordinator, codex_account_id: str) -> None:
    """The turn in flight is work the successor would never learn about."""
    _seed_idle_claude_chat(coordinator.agent_manager, activity_state=ActivityState.THINKING)

    with pytest.raises(HandoffError) as caught:
        coordinator.start_switch(_CHAT_ID, codex_account_id, "op-1")

    assert caught.value.http_status == 409
    assert "Wait for the current turn" in str(caught.value)


def test_switching_a_chat_with_queued_messages_is_a_409(
    coordinator: HandoffCoordinator, codex_account_id: str
) -> None:
    """Queued text belongs to the outgoing agent's queue, which the destroy takes with
    it -- refusing is what keeps the user's unsent words from vanishing."""
    _seed_idle_claude_chat(
        coordinator.agent_manager,
        queued_messages=(QueuedMessageState(queued_id="q1", content="hold on", timestamp="2026-01-01T00:00:00Z"),),
    )

    with pytest.raises(HandoffError) as caught:
        coordinator.start_switch(_CHAT_ID, codex_account_id, "op-1")

    assert caught.value.http_status == 409
    assert "queued" in str(caught.value)


def test_switching_onto_an_unknown_account_is_refused_before_anything_is_frozen(
    coordinator: HandoffCoordinator,
) -> None:
    """Resolution runs on the request thread, so an account this build cannot bind is a
    plain refusal rather than a switch that freezes the chat and then fails."""
    _seed_idle_claude_chat(coordinator.agent_manager)

    with pytest.raises(HandoffError) as caught:
        coordinator.start_switch(_CHAT_ID, "account-that-does-not-exist", "op-1")

    assert caught.value.http_status == 409
    assert coordinator.agent_manager.get_handoff_state(_CHAT_ID) is None


def test_a_second_switch_of_one_chat_is_refused_while_the_first_is_claimed(
    coordinator: HandoffCoordinator, codex_account_id: str
) -> None:
    """Two windows, a double click, and a retried POST all reach here; only one may run."""
    _seed_idle_claude_chat(coordinator.agent_manager)
    assert coordinator._claim(_CHAT_ID, "op-1")

    with pytest.raises(HandoffError) as caught:
        coordinator.start_switch(_CHAT_ID, codex_account_id, "op-2")

    assert caught.value.http_status == 409
    assert "already switching" in str(caught.value)

    # And the slot is reusable once the running switch lets it go, so a failed switch
    # does not wedge the chat out of ever being switched again.
    coordinator._release(_CHAT_ID)
    assert coordinator._claim(_CHAT_ID, "op-3")
    coordinator._release(_CHAT_ID)


def test_a_freeze_that_fails_publishes_a_failure_and_creates_nothing(
    tmp_path: Path, false_binary: str, codex_account_id: str
) -> None:
    """The first step is the freeze, and a failed freeze must stop the switch dead.

    Without the freeze there is no way to stop the outgoing agent taking a turn between
    the snapshot and the handover, so continuing would risk losing a turn -- which is
    strictly worse than not switching. Driven through ``_run_switch`` directly (rather
    than the thread ``start_switch`` spawns) so the raise is observable.
    """
    manager = _build_manager(tmp_path, mngr_binary=false_binary)
    coordinator = HandoffCoordinator(
        agent_manager=manager,
        transcript_archive=TranscriptArchive(archives_dir=None),
        get_watcher=_unused_watcher,
        evict_watcher=_unused_evict,
    )
    _seed_idle_claude_chat(manager)
    try:
        with pytest.raises(HandoffError) as caught:
            coordinator._run_switch(
                chat_id=_CHAT_ID,
                old_agent_id=str(_CHAT_ID),
                old_harness=HarnessType.CLAUDE,
                old_agent_name="Chat-1",
                display_name="Chat 1",
                project_id="",
                account_id=codex_account_id,
                target_harness=HarnessType.CODEX,
                operation_id="op-1",
            )

        assert caught.value.http_status == 500
        state = manager.get_handoff_state(_CHAT_ID)
        assert state is not None
        assert state.phase == HandoffPhase.FAILED
        assert state.target_harness == HarnessType.CODEX
        # The chat still points where it did: nothing was created, nothing committed.
        assert manager.chat_registry.resolve_active_agent_id(_CHAT_ID) == str(_CHAT_ID)
        assert manager.chat_registry.retired_agent_ids(_CHAT_ID) == ()
    finally:
        coordinator.close()
        manager.stop()


def test_no_workspace_means_a_cold_start_rather_than_a_failed_switch(coordinator: HandoffCoordinator) -> None:
    """A chat whose agent has no work dir has nowhere to put a handover file.

    Returning None (not raising) is the deliberate trade: a chat on the harness the user
    asked for, with no handover, beats a chat stuck on the one they asked to leave.
    """
    assert coordinator._write_handover_files(None, "op-1", [], HarnessType.CLAUDE, HarnessType.CODEX) is None


def test_the_handover_message_file_points_at_the_context_file(coordinator: HandoffCoordinator, tmp_path: Path) -> None:
    """The successor's first message is a POINTER, not the transcript itself: it is
    delivered as a real user turn, and pasting a whole history into one would bury the
    conversation the user is trying to continue."""
    message_file = coordinator._write_handover_files(tmp_path, "op-1", [], HarnessType.CLAUDE, HarnessType.CODEX)

    assert message_file is not None
    body = message_file.read_text(encoding="utf-8")
    context_files = [path for path in message_file.parent.iterdir() if path != message_file]
    assert len(context_files) == 1
    assert context_files[0].name in body
