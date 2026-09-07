"""Discover mngr-managed agents using the mngr Python API."""

from __future__ import annotations

import os
from collections.abc import Callable
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from loguru import logger as _loguru_logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.api.find import AgentMatch
from imbue.mngr.api.find import find_all_agents
from imbue.mngr.api.find import find_one_agent
from imbue.mngr.api.find import resolve_to_started_host_and_running_agent
from imbue.mngr.api.list import ErrorBehavior
from imbue.mngr.api.list import list_agents
from imbue.mngr.api.message import MessageResult
from imbue.mngr.api.message import send_key_chord_to_agents
from imbue.mngr.api.message import send_message_to_agents
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.loader import load_config
from imbue.mngr.errors import SendFailureKind
from imbue.mngr.main import get_or_create_plugin_manager
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.utils.env_utils import parse_env_file
from imbue.system_interface.harnesses.harness_type import DEFAULT_HARNESS
from imbue.system_interface.harnesses.harness_type import HarnessType
from imbue.system_interface.harnesses.harness_type import parse_harness

logger = _loguru_logger


def get_host_dir() -> Path:
    """Return the mngr host directory from the environment.

    Falls back to ``~/.mngr`` when ``MNGR_HOST_DIR`` is unset. This is the
    canonical resolver shared by both the API layer (``server._find_agent``)
    and the activity-state tracker (``AgentManager``).
    """
    return Path(os.environ.get("MNGR_HOST_DIR", str(Path.home() / ".mngr")))


class AgentInfo(FrozenModel):
    """Lightweight agent info for the web UI."""

    id: str = Field(description="The agent's unique identifier")
    name: str = Field(description="The agent's human-readable name")
    state: str = Field(description="The agent's lifecycle state (e.g. RUNNING, STOPPED)")
    agent_state_dir: Path = Field(description="Path to the agent's state directory on the local host")
    claude_config_dir: Path = Field(description="Path to the Claude config directory for this agent")
    labels: dict[str, str] = Field(default_factory=dict, description="Agent labels")
    work_dir: str | None = Field(default=None, description="Agent working directory path")
    harness: HarnessType = Field(
        default=DEFAULT_HARNESS,
        description="The agent's harness, narrowed from mngr's AgentDetails.type. Resolved here and nowhere else.",
    )
    create_time: datetime | None = Field(default=None, description="When the agent was created, if known")


def _get_mngr_context() -> tuple[MngrContext, ConcurrencyGroup]:
    # strict=False: a settings file written for a newer mngr than the one this
    # process imported must degrade to a logged warning, not a parse error. This
    # server re-reads `.mngr/settings.toml` through long-lived in-memory code, so
    # during an update the file can briefly be newer than the code -- and a strict
    # parse would turn every agent listing and message send into a 500, taking
    # down the very chat channel needed to finish the update. `mngr config set`
    # and the CLI keep strict parsing; only this live read degrades.
    cg = ConcurrencyGroup(name="system-interface")
    cg.__enter__()
    try:
        pm = get_or_create_plugin_manager()
        mngr_ctx = load_config(pm, cg, is_interactive=False, strict=False)
    except BaseException:
        cg.__exit__(None, None, None)
        raise
    return mngr_ctx, cg


def _read_claude_config_dir_from_env(env_file: Path) -> Path | None:
    """Parse `env_file` and return CLAUDE_CONFIG_DIR if present, else None."""
    if not env_file.exists():
        return None
    try:
        env_vars = parse_env_file(env_file.read_text())
    except OSError:
        logger.debug("Failed to read env file: {}", env_file)
        return None
    value = env_vars.get("CLAUDE_CONFIG_DIR")
    if not value:
        return None
    return Path(value)


def read_claude_config_dir_from_env_file(agent_state_dir: Path) -> Path:
    """Resolve a Claude agent's effective Claude config dir.

    In the current layout no agent or host env file sets CLAUDE_CONFIG_DIR
    at all -- every claude in the workspace resolves claude's own default,
    the shared `~/.claude` -- so the normal outcome is step 4. Steps 1-3
    mirror the env-resolution chain the agent's own tmux session uses at
    startup (mngr sources the host env, then the agent env), so an agent
    that somehow carries an explicit pin (e.g. one created from a shell
    with the var exported) is still watched at the dir it actually uses:

    1. Agent's per-agent env file (`<agent_state_dir>/env`).
    2. Host env file (`$MNGR_HOST_DIR/env`).
    3. Conventional per-agent path (`<agent_state_dir>/plugin/claude/
       anthropic`) if it exists on disk (an isolated mngr_claude agent).
    4. The shared `~/.claude` (claude's default when the var is unset).
    """
    # 1. Per-agent env (an explicitly pinned agent)
    per_agent = _read_claude_config_dir_from_env(agent_state_dir / "env")
    if per_agent is not None:
        return per_agent
    # 2. Host env (nothing writes this anymore; kept as an env-chain mirror)
    host_dir = os.environ.get("MNGR_HOST_DIR", "")
    if host_dir:
        host_level = _read_claude_config_dir_from_env(Path(host_dir) / "env")
        if host_level is not None:
            return host_level
    # 3. Conventional per-agent path (an isolated mngr_claude agent)
    conventional = agent_state_dir / "plugin" / "claude" / "anthropic"
    if conventional.exists():
        return conventional
    # 4. Claude's own default: the shared ~/.claude
    return Path.home() / ".claude"


def discover_agents(
    provider_names: tuple[str, ...] | None = None,
    include_filters: tuple[str, ...] = (),
    exclude_filters: tuple[str, ...] = (),
) -> list[AgentInfo]:
    """List all mngr-managed agents."""
    mngr_ctx, cg = _get_mngr_context()
    try:
        result = list_agents(
            mngr_ctx=mngr_ctx,
            is_streaming=False,
            include_filters=include_filters,
            exclude_filters=exclude_filters,
            provider_names=provider_names,
            error_behavior=ErrorBehavior.CONTINUE,
        )
    finally:
        cg.__exit__(None, None, None)

    # Use default host dir from mngr config for local agents
    default_host_dir = mngr_ctx.config.default_host_dir

    agents: list[AgentInfo] = []
    for agent_details in result.agents:
        agent_id = str(agent_details.id)
        agent_name = str(agent_details.name)
        state = str(agent_details.state.value) if agent_details.state else "unknown"

        # Compute agent state dir from the default host dir
        agent_state_dir = default_host_dir / "agents" / agent_id

        # Get CLAUDE_CONFIG_DIR from the agent's env file
        claude_config_dir = read_claude_config_dir_from_env_file(agent_state_dir)

        agents.append(
            AgentInfo(
                id=agent_id,
                name=agent_name,
                state=state,
                agent_state_dir=agent_state_dir,
                claude_config_dir=claude_config_dir,
                labels=dict(agent_details.labels),
                work_dir=str(agent_details.work_dir),
                harness=parse_harness(str(agent_details.type)),
                create_time=agent_details.create_time,
            )
        )

    return agents


DiscoverFn = Callable[[AgentId, MngrContext], Sequence[AgentMatch]]
SendFn = Callable[[Sequence[AgentMatch], str, MngrContext], MessageResult]
# Press a tmux key token (e.g. "M-q") into a resolved set of agents' panes. Same shape as
# SendFn -- the str is the key token rather than message text.
PressFn = Callable[[Sequence[AgentMatch], str, MngrContext], MessageResult]


def _discover_locations(agent_id: AgentId, mngr_ctx: MngrContext) -> Sequence[AgentMatch]:
    """Resolve an agent id to its location via a full mngr discovery.

    Raises ``AgentNotFoundError`` when the id matches no agent -- ``find_all_agents``
    does not return empty for an unmatched identifier.
    """
    return find_all_agents(
        addresses=(AgentAddress(agent=agent_id),),
        filter_all=False,
        target_state=None,
        mngr_ctx=mngr_ctx,
    )


def _send_to(matches: Sequence[AgentMatch], message: str, mngr_ctx: MngrContext) -> MessageResult:
    """Send a message to a pre-resolved set of agents, auto-starting STOPPED ones."""
    return send_message_to_agents(
        mngr_ctx=mngr_ctx,
        message_content=message,
        agents_to_message=matches,
        error_behavior=ErrorBehavior.CONTINUE,
        is_start_desired=True,
    )


class SendFailedError(Exception):
    """A send was attempted and refused, carrying the reason in the harness's own words.

    Raised on the chat path only, where the reason has somewhere to go: the endpoint turns it
    into the failure the composer shows. The other callers of ``send_message_to_agent`` check
    the returned reason instead, so this changes nothing for them.

    ``kind`` is mngr's classification of the failure, which is what lets the chat decide what to
    offer: trying again can clear a blocked input and cannot conjure back a pane that is gone.
    Unknown when mngr did not classify it, which the chat treats as it always has.
    """

    def __init__(self, detail: str, kind: str = "unknown") -> None:
        self.detail = detail
        self.kind = kind
        super().__init__(detail)


def delivered_or_raise(failure: SendFailure | None) -> bool:
    """Turn a send's reason into an exception, or report delivery.

    ``SessionDeps.send_to_harness`` is typed as returning a bool and is shared with paths that
    have no way to show a reason, so the chat path raises rather than widening that contract.
    The session's send already treats an exception as an expected exit (it resolves its
    in-flight record in a ``finally`` and lets the request fail with the draft kept).
    """
    if failure is not None:
        raise SendFailedError(failure.reason, failure.kind)
    return True


class SendFailure(FrozenModel):
    """Why a send did not land: the harness's own words, plus mngr's classification of them."""

    reason: str
    kind: str


def _first_failure(result: MessageResult) -> SendFailure:
    """Why a send that reached no agent failed, in the harness's own words.

    Each of ``result.failures`` carries the reason alone and mngr's classification of it, so
    nothing here parses prose or strips framing -- the notice supplies its own title and sits in
    the failing agent's own tab. A send can fail with no entry at all (nothing matched the id),
    which is its own answer.
    """
    for failure in result.failures:
        if failure.reason:
            return SendFailure(reason=failure.reason, kind=str(failure.kind))
    # A send can also land and THEN be blocked: the text was accepted and a dialog appeared
    # behind it that mngr could not clear. mngr keeps that apart from a failure -- the message
    # is not lost -- so it is in blocked_agents, and reading only failures above dropped it into
    # the catch-all below. That told the user their agent was unreachable when it was sitting
    # on a dialog, and, because the buttons follow the kind, offered a restart instead of the
    # Retry that answering the dialog makes work.
    for agent_name, blocked_message in result.blocked_agents:
        if blocked_message:
            return SendFailure(
                reason=_without_send_prefix(blocked_message, agent_name), kind=str(SendFailureKind.INPUT_BLOCKED)
            )
    # Nothing matched the id at all, which is its own answer -- and trying again will not change
    # it, so it is classified the same as a pane that is gone.
    return SendFailure(reason="The agent could not be reached.", kind="agent_unreachable")


def _without_send_prefix(message: str, agent_name: str) -> str:
    """Drop mngr's standalone-raise framing from a blocked message before showing it.

    ``blocked_agents`` carries ``str(exception)``, not the bare reason ``failures`` carries, so
    it still has the "Failed to send message to agent X: " a raised error needs and a notice
    titled for that agent does not.
    """
    prefix = f"Failed to send message to agent {agent_name}: "
    return message[len(prefix) :] if message.startswith(prefix) else message


def _press_to(matches: Sequence[AgentMatch], key: str, mngr_ctx: MngrContext) -> MessageResult:
    """Press ``key`` into a pre-resolved set of agents' panes (never auto-starting).

    Unlike a text send, a key chord targets a live turn (there is nothing to flush in a
    stopped agent), so ``is_start_desired`` stays False -- a stopped agent just fails the
    press and the caller reports it, rather than being resurrected to receive a keystroke.
    """
    return send_key_chord_to_agents(
        mngr_ctx=mngr_ctx,
        key=key,
        agents_to_message=matches,
        error_behavior=ErrorBehavior.CONTINUE,
        is_start_desired=False,
    )


class MngrMessenger(FrozenModel):
    """Sends a message to (or presses a key chord into) an agent, preferring a known location.

    Holds the side-effecting mngr collaborators (`discover`, `send`, `press`) as injected
    fields so tests can substitute deterministic fakes without monkeypatching. `AgentManager`
    owns one instance with the real defaults.
    """

    discover: DiscoverFn = _discover_locations
    send: SendFn = _send_to
    press: PressFn = _press_to

    def send_to_agent(
        self, agent_id: AgentId, message: str, known_locations: Sequence[AgentMatch]
    ) -> SendFailure | None:
        """Send to the agent with ``agent_id`` at ``known_locations``, else discovery.

        Returns None when the message was delivered, or the reason it was not. The reason is
        the harness's own words -- "the agent is in shell mode with an unsubmitted command",
        say -- and it exists to be shown to the user, who can usually act on it. Reducing it to
        a bool here would leave the UI with nothing to report but the fact of failure.

        ``known_locations`` (the caller's already-resolved location, from the live
        observe cache) is messaged directly -- no discovery. On a miss, or if that send
        reaches no agent (the location just went stale: destroyed, recreated, or moved
        hosts), it falls back to a full mngr discovery. The id is globally unique, so it
        resolves to exactly the intended agent, never fanning out across same-named
        agents on other hosts. STOPPED agents are auto-started (`is_start_desired=True`).
        """
        mngr_ctx, cg = _get_mngr_context()
        try:
            if known_locations:
                result = self.send(known_locations, message, mngr_ctx)
                if result.successful_agents:
                    return None
            matches = self.discover(agent_id, mngr_ctx)
            result = self.send(matches, message, mngr_ctx)
            if result.successful_agents:
                return None
            return _first_failure(result)
        finally:
            cg.__exit__(None, None, None)

    def press_key_chord_to_agent(self, agent_id: AgentId, key: str, known_locations: Sequence[AgentMatch]) -> bool:
        """Press ``key`` into the agent with ``agent_id`` at ``known_locations``, else discovery.

        The key-chord sibling of ``send_to_agent`` and resolves the agent exactly the same
        way (known location first, discovery on a miss), but delivers a keystroke rather than
        text and never auto-starts a stopped agent. Returns True when the chord reached the
        agent.
        """
        mngr_ctx, cg = _get_mngr_context()
        try:
            if known_locations and self.press(known_locations, key, mngr_ctx).successful_agents:
                return True
            matches = self.discover(agent_id, mngr_ctx)
            return bool(self.press(matches, key, mngr_ctx).successful_agents)
        finally:
            cg.__exit__(None, None, None)


def start_agent(agent_name: str) -> None:
    """Ensure an agent is running, starting it if it is STOPPED.

    This deliberately goes through the *same* in-process mngr path that
    ``MngrMessenger.send_to_agent`` uses to auto-start a STOPPED agent: it loads the mngr
    context exactly the same way (so the same config, env, and cwd apply),
    then resolves the agent and runs mngr's own ``ensure_agent_started``
    (via ``resolve_to_started_host_and_running_agent(..., allow_auto_start=
    True)``). That is what gives us the invariant that opening an agent's
    terminal and sending it a message succeed or fail together -- neither
    reimplements the start, so neither can diverge from the other.

    ``ensure_agent_started`` is a clean no-op for an agent that is already
    running, so this is cheap in the common case (opening the terminal of an
    agent that is already up).

    Raises ``MngrError`` (e.g. ``AgentNotFoundError`` if the agent does
    not exist, or a start failure) -- callers surface these to the user.
    """
    mngr_ctx, cg = _get_mngr_context()
    try:
        address = AgentAddress(agent=AgentName(agent_name))
        host_ref, agent_ref = find_one_agent(address, mngr_ctx)
        resolve_to_started_host_and_running_agent(
            host_ref=host_ref,
            agent_ref=agent_ref,
            allow_auto_start=True,
            mngr_ctx=mngr_ctx,
        )
    finally:
        cg.__exit__(None, None, None)
