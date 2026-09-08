"""Tests for agent_discovery module."""

from collections.abc import Sequence
from pathlib import Path

import pytest

from imbue.mngr.api.find import AgentMatch
from imbue.mngr.api.message import AgentSendFailure
from imbue.mngr.api.message import MessageResult
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import SendFailureKind
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.system_interface.agent_discovery import MngrMessenger
from imbue.system_interface.agent_discovery import _first_failure
from imbue.system_interface.agent_discovery import discover_agents
from imbue.system_interface.agent_discovery import read_claude_config_dir_from_env_file


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
    assert messenger.send_to_agent(_AGENT_ID, "hi", (match,)) is None
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
    assert messenger.send_to_agent(_AGENT_ID, "hi", ()) is None
    assert discover_calls == [_AGENT_ID]
    assert send_calls == [(discovered,)]


@pytest.mark.usefixtures("isolated_mngr_env")
def test_stale_known_location_falls_back_to_discovery() -> None:
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
        reached = [str(m.agent_id) for m in matches if str(m.host_name) == "host-b"]
        return MessageResult(successful_agents=reached)

    messenger = MngrMessenger(discover=_discover, send=_send)
    assert messenger.send_to_agent(_AGENT_ID, "hi", (stale,)) is None
    assert discover_calls == [_AGENT_ID]
    assert send_calls == [(stale,), (fresh,)]


@pytest.mark.usefixtures("isolated_mngr_env")
def test_returns_a_reason_when_nothing_reachable() -> None:
    """A send that reaches no agent reports why, since the reason is what the user can act on."""

    def _discover(agent_id: AgentId, ctx: MngrContext) -> Sequence[AgentMatch]:
        return ()

    def _send(matches: Sequence[AgentMatch], message: str, ctx: MngrContext) -> MessageResult:
        return MessageResult(successful_agents=[])

    messenger = MngrMessenger(discover=_discover, send=_send)
    failure = messenger.send_to_agent(_AGENT_ID, "hi", ())
    assert failure is not None
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
    assert failure is not None
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
