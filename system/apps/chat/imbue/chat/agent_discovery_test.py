"""Tests for agent_discovery module."""

from collections.abc import Callable
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.chat.agent_discovery import COMPACT_UNAVAILABLE_SEND_FAILURE_KIND
from imbue.chat.agent_discovery import COMPACT_WHILE_BUSY_DETAIL
from imbue.chat.agent_discovery import MngrMessenger
from imbue.chat.agent_discovery import SendDelivery
from imbue.chat.agent_discovery import SendFailedError
from imbue.chat.agent_discovery import SendFailure
from imbue.chat.agent_discovery import _first_failure
from imbue.chat.agent_discovery import compact_stale_agents_if_enabled
from imbue.chat.agent_discovery import discover_agents
from imbue.chat.agent_discovery import read_claude_config_dir_from_env_file
from imbue.chat.agent_discovery import request_idle_compaction
from imbue.chat.mock_compaction_agent_test import RecordingCompactionAgent
from imbue.chat.testing import use_mngr_settings
from imbue.mngr.api.find import AgentMatch
from imbue.mngr.api.message import AgentSendFailure
from imbue.mngr.api.message import MessageResult
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import AgentNoLongerIdleError
from imbue.mngr.errors import AgentNotFoundError
from imbue.mngr.errors import MessageLockTimeoutError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import SendFailureKind
from imbue.mngr.errors import SendMessageError
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_autocompact.manager import COMPACTION_MESSAGE_LOCK_TIMEOUT_SECONDS
from imbue.mngr_autocompact.manager import compact_stale_agents_by_name


def test_reads_claude_config_dir_from_env_file(tmp_path: Path) -> None:
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    env_file = agent_state_dir / "env"
    env_file.write_text('CLAUDE_CONFIG_DIR="/custom/config/dir"\n')

    result = read_claude_config_dir_from_env_file(agent_state_dir)

    assert result == Path("/custom/config/dir")


def test_falls_back_to_host_env_when_per_agent_env_lacks_config_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Mirrors the runtime env-resolution chain: the per-agent env file is
    sourced after the host env file, so a CLAUDE_CONFIG_DIR pin in
    $MNGR_HOST_DIR/env applies to any agent whose own env file lacks the var.
    Nothing in the current workspace writes that host-env entry anymore
    (every claude resolves the default ~/.claude), but the layer keeps the
    session_watcher pointed at whatever dir a host-pinned agent actually
    uses."""
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    (host_dir / "env").write_text("MNGR_HOST_DIR=/home/user/.mngr\nCLAUDE_CONFIG_DIR=/shared/claude/config\n")
    agent_state_dir = host_dir / "agents" / "agent-1"
    agent_state_dir.mkdir(parents=True)
    # Per-agent env exists but doesn't carry CLAUDE_CONFIG_DIR.
    (agent_state_dir / "env").write_text("MNGR_AGENT_ID=agent-1\nLATCHKEY_GATEWAY=...\n")

    monkeypatch.setenv("MNGR_HOST_DIR", str(host_dir))
    result = read_claude_config_dir_from_env_file(agent_state_dir)

    assert result == Path("/shared/claude/config")


def test_per_agent_env_takes_precedence_over_host_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When both are set, the per-agent value wins -- matches the runtime
    chain where the agent's env file is sourced AFTER the host env file."""
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    (host_dir / "env").write_text("CLAUDE_CONFIG_DIR=/host/value\n")
    agent_state_dir = host_dir / "agents" / "agent-1"
    agent_state_dir.mkdir(parents=True)
    (agent_state_dir / "env").write_text("CLAUDE_CONFIG_DIR=/per-agent/value\n")

    monkeypatch.setenv("MNGR_HOST_DIR", str(host_dir))
    result = read_claude_config_dir_from_env_file(agent_state_dir)

    assert result == Path("/per-agent/value")


def test_falls_back_to_conventional_path_when_env_file_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("MNGR_HOST_DIR", raising=False)
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    conventional = agent_state_dir / "plugin" / "claude" / "anthropic"
    conventional.mkdir(parents=True)

    result = read_claude_config_dir_from_env_file(agent_state_dir)

    assert result == conventional


def test_falls_back_to_conventional_path_when_env_has_no_config_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("MNGR_HOST_DIR", raising=False)
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    env_file = agent_state_dir / "env"
    env_file.write_text("OTHER_VAR=something\n")
    conventional = agent_state_dir / "plugin" / "claude" / "anthropic"
    conventional.mkdir(parents=True)

    result = read_claude_config_dir_from_env_file(agent_state_dir)

    assert result == conventional


def test_falls_back_to_home_claude_when_nothing_else_exists(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("MNGR_HOST_DIR", raising=False)
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()

    result = read_claude_config_dir_from_env_file(agent_state_dir)

    assert result == Path.home() / ".claude"


@pytest.fixture
def isolated_mngr_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Root mngr config/host at empty tmp dirs so `MngrMessenger` loads a clean context.

    `MngrMessenger.send_to_agent` builds its own `MngrContext` internally; the
    injected `discover`/`send` fakes ignore that context, but it must still load
    without picking up the developer's real project config.
    """
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    monkeypatch.setenv("MNGR_PROJECT_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path / "host"))


_AGENT_ID = AgentId("agent-00000000000000000000000000000001")


def _make_match(agent_id: AgentId = _AGENT_ID, host: str = "host-a") -> AgentMatch:
    return AgentMatch(
        agent_id=agent_id,
        agent_name=AgentName("alpha"),
        host_id=HostId.generate(),
        host_name=HostName(host),
        provider_name=ProviderInstanceName("local"),
    )


@pytest.mark.usefixtures("isolated_mngr_env")
def test_known_location_is_messaged_without_discovery() -> None:
    match = _make_match()
    discover_calls: list[AgentId] = []
    send_calls: list[tuple[AgentMatch, ...]] = []

    def _discover(agent_id: AgentId, ctx: MngrContext) -> Sequence[AgentMatch]:
        discover_calls.append(agent_id)
        return ()

    def _send(matches: Sequence[AgentMatch], message: str, ctx: MngrContext) -> MessageResult:
        send_calls.append(tuple(matches))
        return MessageResult(successful_agents=[str(m.agent_id) for m in matches])

    messenger = MngrMessenger(discover=_discover, send=_send)
    assert messenger.send_to_agent(_AGENT_ID, "hi", (match,)) is SendDelivery.DELIVERED
    assert discover_calls == []
    assert send_calls == [(match,)]


@pytest.mark.usefixtures("isolated_mngr_env")
def test_empty_known_locations_falls_back_to_discovery() -> None:
    discovered = _make_match()
    discover_calls: list[AgentId] = []
    send_calls: list[tuple[AgentMatch, ...]] = []

    def _discover(agent_id: AgentId, ctx: MngrContext) -> Sequence[AgentMatch]:
        discover_calls.append(agent_id)
        return (discovered,)

    def _send(matches: Sequence[AgentMatch], message: str, ctx: MngrContext) -> MessageResult:
        send_calls.append(tuple(matches))
        return MessageResult(successful_agents=[str(m.agent_id) for m in matches])

    messenger = MngrMessenger(discover=_discover, send=_send)
    assert messenger.send_to_agent(_AGENT_ID, "hi", ()) is SendDelivery.DELIVERED
    assert discover_calls == [_AGENT_ID]
    assert send_calls == [(discovered,)]


def _not_found_on_host() -> MessageResult:
    return MessageResult(
        failures=[
            AgentSendFailure(
                agent_name="alpha", reason="Agent not found on host host-a", kind=SendFailureKind.AGENT_NOT_FOUND
            )
        ]
    )


@pytest.mark.usefixtures("isolated_mngr_env")
@pytest.mark.parametrize(
    "stale_result",
    [_not_found_on_host, MessageResult],
    ids=["agent-not-found", "matched-nothing"],
)
def test_stale_known_location_falls_back_to_discovery(stale_result: Callable[[], MessageResult]) -> None:
    """A known location that does not lead to the agent is looked up afresh and sent to once more.

    Nothing was typed at the stale location, so the one resend cannot deliver the message twice.
    """
    stale = _make_match(host="host-a")
    fresh = _make_match(host="host-b")
    discover_calls: list[AgentId] = []
    send_calls: list[tuple[AgentMatch, ...]] = []

    def _discover(agent_id: AgentId, ctx: MngrContext) -> Sequence[AgentMatch]:
        discover_calls.append(agent_id)
        return (fresh,)

    def _send(matches: Sequence[AgentMatch], message: str, ctx: MngrContext) -> MessageResult:
        send_calls.append(tuple(matches))
        # The stale location reaches no agent; the freshly discovered one does.
        if all(str(m.host_name) == "host-a" for m in matches):
            return stale_result()
        return MessageResult(successful_agents=[str(m.agent_id) for m in matches])

    messenger = MngrMessenger(discover=_discover, send=_send)
    assert messenger.send_to_agent(_AGENT_ID, "hi", (stale,)) is SendDelivery.DELIVERED
    assert discover_calls == [_AGENT_ID]
    assert send_calls == [(stale,), (fresh,)]


def _submission_unconfirmed() -> MessageResult:
    return MessageResult(
        failures=[
            AgentSendFailure(
                agent_name="alpha",
                reason="Timeout waiting for message submission evidence: no evidence within 90s",
                kind=SendFailureKind.SUBMISSION_UNCONFIRMED,
            )
        ]
    )


def _unclassified_failure() -> MessageResult:
    return MessageResult(
        failures=[AgentSendFailure(agent_name="alpha", reason="tmux send-keys failed", kind=SendFailureKind.UNKNOWN)]
    )


def _would_not_start() -> MessageResult:
    return MessageResult(
        failures=[
            AgentSendFailure(agent_name="alpha", reason="start timed out", kind=SendFailureKind.AGENT_UNREACHABLE)
        ]
    )


def _not_ready() -> MessageResult:
    return MessageResult(
        failures=[AgentSendFailure(agent_name="alpha", reason="still starting", kind=SendFailureKind.NOT_READY)]
    )


def _state_unknown() -> MessageResult:
    return MessageResult(
        failures=[AgentSendFailure(agent_name="alpha", reason="host too loaded", kind=SendFailureKind.STATE_UNKNOWN)]
    )


def _delivered_but_blocked() -> MessageResult:
    return MessageResult(blocked_agents=[("alpha", "Claude is waiting for you to confirm a model switch.")])


def _not_found_beside_unconfirmed() -> MessageResult:
    result = _submission_unconfirmed()
    result.failures.extend(_not_found_on_host().failures)
    return result


@pytest.mark.usefixtures("isolated_mngr_env")
@pytest.mark.parametrize(
    ("known_location_result", "expected_kind"),
    [
        (_delivered_but_blocked, "input_blocked"),
        (_not_found_beside_unconfirmed, "submission_unconfirmed"),
    ],
    ids=["delivered-but-blocked", "not-found-beside-unconfirmed"],
)
def test_a_failure_another_send_cannot_help_is_returned_as_it_is(
    known_location_result: Callable[[], MessageResult], expected_kind: str
) -> None:
    """A blocked send was delivered, so sending again could only deliver it twice. A send that was unconfirmed at one
    agent and missed another is not the plain ambiguity a resend answers.
    """
    discover_calls: list[AgentId] = []
    send_calls: list[tuple[AgentMatch, ...]] = []
    known = _make_match()

    def _discover(agent_id: AgentId, ctx: MngrContext) -> Sequence[AgentMatch]:
        discover_calls.append(agent_id)
        return (_make_match(host="host-b"),)

    def _send(matches: Sequence[AgentMatch], message: str, ctx: MngrContext) -> MessageResult:
        send_calls.append(tuple(matches))
        return known_location_result()

    messenger = MngrMessenger(discover=_discover, send=_send)
    failure = messenger.send_to_agent(_AGENT_ID, "hi", (known,))
    assert isinstance(failure, SendFailure)
    assert failure.kind == expected_kind
    assert discover_calls == []
    assert send_calls == [(known,)]


def _not_submitted() -> MessageResult:
    return MessageResult(
        failures=[
            AgentSendFailure(
                agent_name="alpha",
                reason="the message never left the agent's input box, so it was cleared from there",
                kind=SendFailureKind.NOT_SUBMITTED,
            )
        ]
    )


@pytest.mark.usefixtures("isolated_mngr_env")
@pytest.mark.parametrize(
    ("second_result", "expected_kind"),
    [(MessageResult, None), (_not_submitted, "not_submitted")],
    ids=["taken-the-second-time", "untaken-again"],
)
def test_a_message_the_agent_never_took_is_sent_there_once_more(
    second_result: Callable[[], MessageResult], expected_kind: str | None
) -> None:
    """The agent cleared the untaken message from its input box, so one more send delivers it once."""
    known = _make_match()
    send_calls: list[tuple[AgentMatch, ...]] = []
    results = [_not_submitted()]

    def _discover(agent_id: AgentId, ctx: MngrContext) -> Sequence[AgentMatch]:
        raise AssertionError("an untaken message is sent where the agent is, not looked up again")

    def _send(matches: Sequence[AgentMatch], message: str, ctx: MngrContext) -> MessageResult:
        send_calls.append(tuple(matches))
        if results:
            return results.pop()
        result = second_result()
        return result if result.failures else MessageResult(successful_agents=[str(m.agent_id) for m in matches])

    messenger = MngrMessenger(discover=_discover, send=_send)
    failure = messenger.send_to_agent(_AGENT_ID, "hi", (known,))
    assert (failure.kind if isinstance(failure, SendFailure) else None) == expected_kind
    assert send_calls == [(known,), (known,)]


@pytest.mark.usefixtures("isolated_mngr_env")
def test_a_message_the_agent_never_took_at_a_discovered_location_is_sent_there_once_more() -> None:
    """With no known location the agent is looked up, and an untaken send there gets its one more send too."""
    discovered = _make_match(host="host-b")
    send_calls: list[tuple[AgentMatch, ...]] = []
    results = [_not_submitted()]

    def _discover(agent_id: AgentId, ctx: MngrContext) -> Sequence[AgentMatch]:
        return (discovered,)

    def _send(matches: Sequence[AgentMatch], message: str, ctx: MngrContext) -> MessageResult:
        send_calls.append(tuple(matches))
        if results:
            return results.pop()
        return MessageResult(successful_agents=[str(m.agent_id) for m in matches])

    messenger = MngrMessenger(discover=_discover, send=_send)
    assert messenger.send_to_agent(_AGENT_ID, "hi", ()) is SendDelivery.DELIVERED
    assert send_calls == [(discovered,), (discovered,)]


def _delivered_to(matches: Sequence[AgentMatch]) -> MessageResult:
    return MessageResult(successful_agents=[str(match.agent_id) for match in matches])


@pytest.mark.usefixtures("isolated_mngr_env")
@pytest.mark.parametrize(
    "unsure_result", [_submission_unconfirmed, _unclassified_failure], ids=["unconfirmed", "unclassified"]
)
def test_a_message_that_may_not_have_arrived_is_sent_there_once_more_and_reported_as_resent(
    unsure_result: Callable[[], MessageResult],
) -> None:
    """Nothing shows whether the agent took it, so it is sent again where it was sent, and the chat says so."""
    known = _make_match()
    send_calls: list[tuple[AgentMatch, ...]] = []
    results = [unsure_result()]

    def _discover(agent_id: AgentId, ctx: MngrContext) -> Sequence[AgentMatch]:
        raise AssertionError("an unconfirmed message is sent where the agent is, not looked up again")

    def _send(matches: Sequence[AgentMatch], message: str, ctx: MngrContext) -> MessageResult:
        send_calls.append(tuple(matches))
        return results.pop() if results else _delivered_to(matches)

    messenger = MngrMessenger(discover=_discover, send=_send)
    assert messenger.send_to_agent(_AGENT_ID, "hi", (known,)) is SendDelivery.RESENT
    assert send_calls == [(known,), (known,)]


@pytest.mark.usefixtures("isolated_mngr_env")
@pytest.mark.parametrize(
    "unsure_result", [_submission_unconfirmed, _unclassified_failure], ids=["unconfirmed", "unclassified"]
)
@pytest.mark.parametrize(
    ("second_result", "expected_kind"),
    [
        (_submission_unconfirmed, "submission_unconfirmed"),
        (_unclassified_failure, "unknown"),
        (_not_found_on_host, "agent_not_found"),
        (_not_submitted, "not_submitted"),
    ],
    ids=["unconfirmed-again", "unclassified-the-second-time", "not-found-the-second-time", "untaken-the-second-time"],
)
def test_a_message_that_may_not_have_arrived_is_never_sent_a_third_time(
    unsure_result: Callable[[], MessageResult], second_result: Callable[[], MessageResult], expected_kind: str
) -> None:
    """The second send's failure is the answer: no further send, and no lookup that would lead to one."""
    known = _make_match()
    send_calls: list[tuple[AgentMatch, ...]] = []
    results = [second_result(), unsure_result()]

    def _discover(agent_id: AgentId, ctx: MngrContext) -> Sequence[AgentMatch]:
        raise AssertionError("a message sent twice is not looked up and sent a third time")

    def _send(matches: Sequence[AgentMatch], message: str, ctx: MngrContext) -> MessageResult:
        send_calls.append(tuple(matches))
        return results.pop()

    messenger = MngrMessenger(discover=_discover, send=_send)
    failure = messenger.send_to_agent(_AGENT_ID, "hi", (known,))
    assert isinstance(failure, SendFailure)
    assert failure.kind == expected_kind
    assert send_calls == [(known,), (known,)]


@pytest.mark.usefixtures("isolated_mngr_env")
def test_an_unconfirmed_message_at_a_discovered_location_is_sent_there_once_more() -> None:
    discovered = _make_match(host="host-b")
    send_calls: list[tuple[AgentMatch, ...]] = []
    results = [_submission_unconfirmed()]

    def _discover(agent_id: AgentId, ctx: MngrContext) -> Sequence[AgentMatch]:
        return (discovered,)

    def _send(matches: Sequence[AgentMatch], message: str, ctx: MngrContext) -> MessageResult:
        send_calls.append(tuple(matches))
        return results.pop() if results else _delivered_to(matches)

    messenger = MngrMessenger(discover=_discover, send=_send)
    assert messenger.send_to_agent(_AGENT_ID, "hi", ()) is SendDelivery.RESENT
    assert send_calls == [(discovered,), (discovered,)]


@pytest.mark.usefixtures("isolated_mngr_env")
def test_a_message_untaken_and_then_unconfirmed_is_sent_once_more_after_each() -> None:
    """The untaken first send delivered nothing, so the unconfirmed second is the only copy that may have landed."""
    known = _make_match()
    send_calls: list[tuple[AgentMatch, ...]] = []
    results = [_submission_unconfirmed(), _not_submitted()]

    def _discover(agent_id: AgentId, ctx: MngrContext) -> Sequence[AgentMatch]:
        raise AssertionError("the message is sent where the agent is, not looked up again")

    def _send(matches: Sequence[AgentMatch], message: str, ctx: MngrContext) -> MessageResult:
        send_calls.append(tuple(matches))
        return results.pop() if results else _delivered_to(matches)

    messenger = MngrMessenger(discover=_discover, send=_send)
    assert messenger.send_to_agent(_AGENT_ID, "hi", (known,)) is SendDelivery.RESENT
    assert send_calls == [(known,), (known,), (known,)]


@pytest.mark.usefixtures("isolated_mngr_env")
@pytest.mark.parametrize(
    ("unsure_result", "expected_kind"),
    [(_submission_unconfirmed, "submission_unconfirmed"), (_unclassified_failure, "unknown")],
    ids=["unconfirmed", "unclassified"],
)
def test_a_message_that_may_not_have_arrived_is_returned_as_is_when_its_caller_sends_it_again_itself(
    unsure_result: Callable[[], MessageResult], expected_kind: str
) -> None:
    known = _make_match()
    send_calls: list[tuple[AgentMatch, ...]] = []

    def _discover(agent_id: AgentId, ctx: MngrContext) -> Sequence[AgentMatch]:
        raise AssertionError("such a message is not looked up again")

    def _send(matches: Sequence[AgentMatch], message: str, ctx: MngrContext) -> MessageResult:
        send_calls.append(tuple(matches))
        return unsure_result()

    messenger = MngrMessenger(discover=_discover, send=_send)
    failure = messenger.send_to_agent(_AGENT_ID, "hi", (known,), is_resent_when_unsure=False)
    assert isinstance(failure, SendFailure)
    assert failure.kind == expected_kind
    assert send_calls == [(known,)]


@pytest.mark.usefixtures("isolated_mngr_env")
@pytest.mark.parametrize(
    "first_result",
    [_not_ready, _state_unknown, _not_submitted, _would_not_start, _submission_unconfirmed, _unclassified_failure],
    ids=["not-ready", "state-unknown", "not-submitted", "would-not-start", "unconfirmed", "unclassified"],
)
def test_a_send_a_stop_took_back_is_not_tried_again(first_result: Callable[[], MessageResult]) -> None:
    known = _make_match()
    send_calls: list[tuple[AgentMatch, ...]] = []

    def _discover(agent_id: AgentId, ctx: MngrContext) -> Sequence[AgentMatch]:
        raise AssertionError("the message was handed back, so the agent is not looked up either")

    def _send(matches: Sequence[AgentMatch], message: str, ctx: MngrContext) -> MessageResult:
        send_calls.append(tuple(matches))
        return first_result()

    messenger = MngrMessenger(discover=_discover, send=_send, pause=lambda _seconds: None)
    outcome = messenger.send_to_agent(_AGENT_ID, "hi", (known,), is_taken_back=lambda: True)
    assert isinstance(outcome, SendFailure)
    assert outcome.kind == first_result().failures[0].kind
    assert send_calls == [(known,)]


@pytest.mark.usefixtures("isolated_mngr_env")
def test_a_send_a_stop_took_back_after_it_missed_the_agent_is_not_looked_up_again() -> None:
    def _discover(agent_id: AgentId, ctx: MngrContext) -> Sequence[AgentMatch]:
        raise AssertionError("the message was handed back, so the agent is not looked up")

    messenger = MngrMessenger(
        discover=_discover,
        send=lambda matches, message, ctx: _not_found_on_host(),
        pause=lambda _seconds: None,
    )
    outcome = messenger.send_to_agent(_AGENT_ID, "hi", (_make_match(),), is_taken_back=lambda: True)
    assert isinstance(outcome, SendFailure)
    assert outcome.kind == SendFailureKind.AGENT_NOT_FOUND


@pytest.mark.usefixtures("isolated_mngr_env")
@pytest.mark.parametrize("waiting_result", [_not_ready, _state_unknown], ids=["not-ready", "state-unknown"])
def test_a_send_nothing_could_type_yet_is_tried_again_after_a_growing_wait(
    waiting_result: Callable[[], MessageResult],
) -> None:
    known = _make_match()
    send_calls: list[tuple[AgentMatch, ...]] = []
    waits: list[float] = []
    results = [waiting_result(), waiting_result()]

    def _discover(agent_id: AgentId, ctx: MngrContext) -> Sequence[AgentMatch]:
        raise AssertionError("the agent is where it was, so it is not looked up again")

    def _send(matches: Sequence[AgentMatch], message: str, ctx: MngrContext) -> MessageResult:
        send_calls.append(tuple(matches))
        return results.pop() if results else _delivered_to(matches)

    messenger = MngrMessenger(discover=_discover, send=_send, pause=waits.append)
    assert messenger.send_to_agent(_AGENT_ID, "hi", (known,)) is SendDelivery.DELIVERED
    assert send_calls == [(known,)] * 3
    assert waits == [1.0, 2.0]


@pytest.mark.usefixtures("isolated_mngr_env")
@pytest.mark.parametrize(
    ("waiting_result", "expected_kind"),
    [(_not_ready, "not_ready"), (_state_unknown, "state_unknown")],
    ids=["not-ready", "state-unknown"],
)
def test_a_send_that_stays_impossible_is_returned_as_the_failure_after_the_last_wait(
    waiting_result: Callable[[], MessageResult], expected_kind: str
) -> None:
    known = _make_match()
    send_calls: list[tuple[AgentMatch, ...]] = []
    waits: list[float] = []

    def _discover(agent_id: AgentId, ctx: MngrContext) -> Sequence[AgentMatch]:
        raise AssertionError("the agent is where it was, so it is not looked up again")

    def _send(matches: Sequence[AgentMatch], message: str, ctx: MngrContext) -> MessageResult:
        send_calls.append(tuple(matches))
        return waiting_result()

    messenger = MngrMessenger(discover=_discover, send=_send, pause=waits.append)
    failure = messenger.send_to_agent(_AGENT_ID, "hi", (known,))
    assert isinstance(failure, SendFailure)
    assert failure.kind == expected_kind
    assert send_calls == [(known,)] * 4
    assert waits == [1.0, 2.0, 4.0]


@pytest.mark.usefixtures("isolated_mngr_env")
@pytest.mark.parametrize(
    ("second_result", "expected_kind"),
    [(MessageResult, None), (_would_not_start, "agent_unreachable")],
    ids=["started-the-second-time", "would-not-start-again"],
)
def test_a_send_to_an_agent_that_would_not_start_is_tried_once_more(
    second_result: Callable[[], MessageResult], expected_kind: str | None
) -> None:
    known = _make_match()
    send_calls: list[tuple[AgentMatch, ...]] = []
    results = [second_result(), _would_not_start()]

    def _discover(agent_id: AgentId, ctx: MngrContext) -> Sequence[AgentMatch]:
        raise AssertionError("the agent is where it was, so it is not looked up again")

    def _send(matches: Sequence[AgentMatch], message: str, ctx: MngrContext) -> MessageResult:
        send_calls.append(tuple(matches))
        result = results.pop()
        return _delivered_to(matches) if not result.failures else result

    messenger = MngrMessenger(discover=_discover, send=_send)
    outcome = messenger.send_to_agent(_AGENT_ID, "hi", (known,))
    if expected_kind is None:
        assert outcome is SendDelivery.DELIVERED
    else:
        assert isinstance(outcome, SendFailure)
        assert outcome.kind == expected_kind
    assert send_calls == [(known,), (known,)]


@pytest.mark.usefixtures("isolated_mngr_env")
def test_a_send_that_waited_and_then_went_unconfirmed_is_sent_once_more() -> None:
    """The waits typed nothing, so the unconfirmed send is the only copy that may have landed."""
    known = _make_match()
    send_calls: list[tuple[AgentMatch, ...]] = []
    results = [_submission_unconfirmed(), _not_ready()]

    def _discover(agent_id: AgentId, ctx: MngrContext) -> Sequence[AgentMatch]:
        raise AssertionError("the agent is where it was, so it is not looked up again")

    def _send(matches: Sequence[AgentMatch], message: str, ctx: MngrContext) -> MessageResult:
        send_calls.append(tuple(matches))
        return results.pop() if results else _delivered_to(matches)

    messenger = MngrMessenger(discover=_discover, send=_send, pause=lambda _seconds: None)
    assert messenger.send_to_agent(_AGENT_ID, "hi", (known,)) is SendDelivery.RESENT
    assert send_calls == [(known,)] * 3


@pytest.mark.usefixtures("isolated_mngr_env")
def test_returns_a_reason_when_nothing_reachable() -> None:
    """A send that reaches no agent reports why, since the reason is what the user can act on."""

    def _discover(agent_id: AgentId, ctx: MngrContext) -> Sequence[AgentMatch]:
        return ()

    def _send(matches: Sequence[AgentMatch], message: str, ctx: MngrContext) -> MessageResult:
        return MessageResult(successful_agents=[])

    messenger = MngrMessenger(discover=_discover, send=_send)
    failure = messenger.send_to_agent(_AGENT_ID, "hi", ())
    assert isinstance(failure, SendFailure)
    assert failure.reason == "The agent could not be reached."
    # Nothing matched the id, and trying again will not change that.
    assert failure.kind == "agent_unreachable"


@pytest.mark.usefixtures("isolated_mngr_env")
def test_reports_the_harness_reason_for_a_refused_send() -> None:
    """mngr's own words are what come back, not a generic failure.

    The harness knows what it is blocked on and says so in terms the user can act on -- which
    key resolves the dialog holding its input, say. Reducing that to a bool here would leave
    the chat with nothing to report but the fact that something went wrong.
    """
    refusal = "The agent is in shell mode with an unsubmitted command."

    def _discover(agent_id: AgentId, ctx: MngrContext) -> Sequence[AgentMatch]:
        return (_make_match(),)

    def _send(matches: Sequence[AgentMatch], message: str, ctx: MngrContext) -> MessageResult:
        return MessageResult(
            successful_agents=[],
            failures=[AgentSendFailure(agent_name="alpha", reason=refusal, kind=SendFailureKind.INPUT_BLOCKED)],
        )

    messenger = MngrMessenger(discover=_discover, send=_send)
    failure = messenger.send_to_agent(_AGENT_ID, "hi", ())
    assert isinstance(failure, SendFailure)
    assert failure.reason == refusal
    # mngr classified it, and that classification comes through beside the words.
    assert failure.kind == "input_blocked"


def test_a_delivered_but_blocked_send_reports_the_dialog_not_an_unreachable_agent() -> None:
    """mngr keeps "landed, then blocked" apart from "never landed"; the notice must too.

    Reading only ``failures`` dropped this into the unreachable catch-all, which was false --
    the agent is sitting on a dialog -- and, because the recovery buttons follow the kind,
    withheld the Retry that answering the dialog makes work while offering a restart.
    """
    result = MessageResult()
    result.blocked_agents.append(
        (
            "alpha",
            "Failed to send message to agent alpha: Claude is waiting for you to confirm a model switch."
            " Answer it in the agent's terminal.",
        )
    )
    failure = _first_failure(result)
    assert failure.kind == "input_blocked"
    assert failure.reason.startswith("Claude is waiting for you to confirm a model switch.")
    assert "Failed to send message to agent" not in failure.reason


def test_a_real_failure_still_outranks_a_blocked_one() -> None:
    """A send that never landed is the more urgent truth, so it is reported first."""
    result = MessageResult()
    result.failures.append(
        AgentSendFailure(agent_name="alpha", reason="the pane is gone", kind=SendFailureKind.AGENT_UNREACHABLE)
    )
    result.blocked_agents.append(("alpha", "a dialog is up"))
    failure = _first_failure(result)
    assert failure.reason == "the pane is gone"
    assert failure.kind == "agent_unreachable"


def test_nothing_matched_is_still_the_unreachable_catch_all() -> None:
    failure = _first_failure(MessageResult())
    assert failure.kind == "agent_unreachable"
    assert failure.reason == "The agent could not be reached."


def test_unknown_config_field_degrades_to_a_warning_not_a_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, loguru_records: list[str]
) -> None:
    """A settings file written for a newer mngr must not lock this server out.

    During an update the on-disk `.mngr/settings.toml` can briefly be newer than
    the mngr this long-lived process imported. Under strict parsing every read
    path through `_get_mngr_context` -- listing agents, sending a message --
    became a 500, which took down the very chat channel needed to finish the
    update. The live read is therefore non-strict: the unknown field is dropped
    with a logged warning and agents still list.
    """
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    (config_dir / "settings.toml").write_text('is_allowed_in_pytest = true\nfield_from_a_newer_mngr = "surprise"\n')
    monkeypatch.setenv("MNGR_PROJECT_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path / "host"))

    # Local provider only: the point is the config-parse path every listing
    # goes through, not remote-provider discovery.
    agents = discover_agents(provider_names=("local",))

    # The listing survived (a fresh empty host dir simply has no agents), and
    # the unknown field was reported rather than swallowed silently.
    assert agents == []
    assert any("field_from_a_newer_mngr" in record for record in loguru_records)


def test_compacting_agents_that_no_longer_exist_compacts_nothing_and_raises_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The sweep lists its chats a moment before this call, so a name may already be gone."""
    use_mngr_settings('[plugins.autocompact]\nmode = "proactive_timer"\n', tmp_path, monkeypatch)

    assert compact_stale_agents_if_enabled(["chat-that-was-destroyed"], compact_stale_agents_by_name) == []


@pytest.mark.parametrize(
    ("settings", "expected_batches", "expected_compacted"),
    [
        ("", [], []),
        ('[plugins.autocompact]\nmode = "on_next_prompt"\n', [], []),
        ('[plugins.autocompact]\nmode = "proactive_timer"\n', [["chat-alpha", "chat-beta"]], ["chat-alpha"]),
    ],
)
def test_the_plugin_is_called_only_when_the_workspace_mngr_config_has_proactive_compaction_on(
    settings: str,
    expected_batches: list[list[str]],
    expected_compacted: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mode is loaded through mngr's own loader, so the sweep acts exactly when the plugin would."""
    use_mngr_settings(settings, tmp_path, monkeypatch)
    recorded_batches: list[list[str]] = []

    def compact_by_name(mngr_ctx: MngrContext, names: Sequence[AgentName]) -> Sequence[AgentName]:
        recorded_batches.append([str(name) for name in names])
        return names[:1]

    compacted = compact_stale_agents_if_enabled(["chat-alpha", "chat-beta"], compact_by_name)

    assert recorded_batches == expected_batches
    assert compacted == expected_compacted


_IDLE_SINCE = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


def test_an_idle_agent_is_compacted_with_its_idle_start_rechecked_under_a_bounded_lock() -> None:
    agent = RecordingCompactionAgent(idle_since=_IDLE_SINCE)

    request_idle_compaction(agent, "keep the test plan")

    assert agent.requests == [("keep the test plan", COMPACTION_MESSAGE_LOCK_TIMEOUT_SECONDS, _IDLE_SINCE)]


@pytest.mark.parametrize(
    ("idle_since", "error"),
    [
        pytest.param(None, None, id="mid_turn"),
        pytest.param(
            _IDLE_SINCE, AgentNoLongerIdleError("agent", _IDLE_SINCE, None), id="started_a_turn_before_the_lock"
        ),
        pytest.param(_IDLE_SINCE, MessageLockTimeoutError("agent", 5.0), id="another_send_holds_the_lock"),
    ],
)
def test_an_agent_that_is_not_idle_is_declined_rather_than_compacted(
    idle_since: datetime | None, error: MngrError | None
) -> None:
    agent = RecordingCompactionAgent(idle_since=idle_since, error=error)

    with pytest.raises(SendFailedError) as raised:
        request_idle_compaction(agent, None)

    assert (raised.value.detail, raised.value.kind) == (
        COMPACT_WHILE_BUSY_DETAIL,
        COMPACT_UNAVAILABLE_SEND_FAILURE_KIND,
    )


@pytest.mark.usefixtures("isolated_mngr_env")
def test_a_compaction_request_reaches_mngr_with_its_instructions() -> None:
    calls: list[tuple[AgentId, str | None]] = []

    def _compact(agent_id: AgentId, instructions: str | None, ctx: MngrContext) -> None:
        calls.append((agent_id, instructions))

    MngrMessenger(compact=_compact).request_compaction(_AGENT_ID, "keep the test plan")

    assert calls == [(_AGENT_ID, "keep the test plan")]


@pytest.mark.usefixtures("isolated_mngr_env")
def test_a_failed_compaction_request_is_a_send_failure_in_mngrs_words() -> None:
    def _compact(agent_id: AgentId, instructions: str | None, ctx: MngrContext) -> None:
        raise SendMessageError(str(agent_id), "failed to request context compaction: the daemon hung up")

    with pytest.raises(SendFailedError) as raised:
        MngrMessenger(compact=_compact).request_compaction(_AGENT_ID, None)

    assert "the daemon hung up" in raised.value.detail
    assert raised.value.kind == "unknown"


@pytest.mark.usefixtures("isolated_mngr_env")
def test_a_compaction_request_for_an_agent_mngr_no_longer_lists_raises_not_found() -> None:
    with pytest.raises(AgentNotFoundError):
        MngrMessenger().request_compaction(AgentId("agent-0000000000000000000000000000dead"), None)
