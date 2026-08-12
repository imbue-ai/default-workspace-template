"""Codex's model catalog and its model resolver.

Codex drives the stock ``codex app-server``: a model/effort/fast change is
``thread/settings/update`` over the per-agent daemon, and the daemon echoes the
effective settings back on ``thread/settings/updated``. The live model chip is
kept uniform: the ledger (:mod:`~imbue.system_interface.harnesses.codex.ledger`)
mirrors each ``thread/settings/updated`` to the agent's
``minds_model_state.json`` -- ``{"model", "effort", "fast"}``, the uniform
live-state schema -- and the shared reader
(:func:`~imbue.system_interface.harnesses.model.read_model_identity`) parses that
file via the harness's registered relative path (``plugin/codex/home``, i.e.
under CODEX_HOME). That is the event-driven replacement for the retired
``codex-in-minds`` fork's disk write; nothing else writes the file for codex.

This resolver owns only the WRITE (switch) side: it applies a selection via
``thread/settings/update`` on a short-lived per-switch connection to the daemon,
NOT by typing ``/model`` / ``/fast`` into the pane (the stock binary has no such
commands over the app-server drive). When no ``thread/settings/updated`` has been
mirrored yet the file is absent, the reader returns ``None``, and the bar shows
logo-only.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any
from typing import Protocol

from loguru import logger

from imbue.mngr_codex.app_server_client import CodexAppServerClient
from imbue.mngr_codex.app_server_client import CodexAppServerError
from imbue.mngr_codex.app_server_client import connect_app_server_transport
from imbue.mngr_codex.codex_config import APP_SERVER_THREAD_FILENAME
from imbue.mngr_codex.codex_config import CODEX_HOME_RELATIVE_PATH
from imbue.mngr_codex.codex_config import get_codex_app_server_socket_path
from imbue.mngr_codex.codex_config import get_codex_home
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.model import EffortChoice
from imbue.system_interface.harnesses.model import HarnessCatalog
from imbue.system_interface.harnesses.model import HarnessModelResolver
from imbue.system_interface.harnesses.model import ModelAxis
from imbue.system_interface.harnesses.model import ModelIdentity
from imbue.system_interface.harnesses.model import ModelOption
from imbue.system_interface.harnesses.model import PickerMode
from imbue.system_interface.harnesses.model import SwitchMode
from imbue.system_interface.harnesses.model import SwitchResult

# Codex efforts: low..xhigh shown; max/ultra declared-but-hidden (valid + matchable,
# never offered). Plain strings, as the catalog carries them.
_CODEX_EFFORTS: tuple[EffortChoice, ...] = (
    EffortChoice(level="low"),
    EffortChoice(level="medium"),
    EffortChoice(level="high"),
    EffortChoice(level="xhigh"),
    EffortChoice(level="max", in_picker=False),
    EffortChoice(level="ultra", in_picker=False),
)

CODEX_CATALOG: HarnessCatalog = HarnessCatalog(
    options=(
        ModelOption(id="gpt-5.6-sol", label="GPT-5.6-Sol", efforts=_CODEX_EFFORTS, supports_fast=True),
        ModelOption(id="gpt-5.6-terra", label="GPT-5.6-Terra", efforts=_CODEX_EFFORTS, supports_fast=True),
        ModelOption(id="gpt-5.6-luna", label="GPT-5.6-Luna", efforts=_CODEX_EFFORTS, supports_fast=True),
        ModelOption(id="gpt-5.5", label="GPT-5.5", efforts=_CODEX_EFFORTS, supports_fast=True),
        ModelOption(id="gpt-5.2", label="GPT-5.2", efforts=_CODEX_EFFORTS, supports_fast=True),
    ),
    # EAGER_THEN_RECONCILE: switch() applies thread/settings/update over the app-server and the
    # daemon echoes thread/settings/updated, which the ledger mirrors to minds_model_state.json in
    # well under a second; the shared reader reconciles the chip. That round-trip is fast and
    # reliable enough to move the chip optimistically on click and snap it to the pushed live choice
    # a beat later (the frontend's 5-minute pending fallback only fires if the switch never lands).
    # A model the account cannot use is not an RPC error -- the daemon falls back and echoes the
    # effective value, which the mirror reconciles -- so a failed switch is a silent no-op (no popup).
    switch_mode=SwitchMode.EAGER_THEN_RECONCILE,
    picker_mode=PickerMode.LIST,
    powered_by_label="Codex",
    # Codex's patched binary watches shoulder_tap_atomic.jsonl and merges parked steer
    # messages into the live turn (ABA-gated on the turn id), so the "Shoulder tap" button
    # can flush atomically without a restart. Only codex supports this today.
    native_atomic_shoulder_tap_possible=True,
)

# Codex writes its live state under CODEX_HOME (``<state_dir>/plugin/codex/home``), not at
# the state-dir root, so the shared reader/watch path takes this relative directory as data.
CODEX_STATE_RELATIVE_PATH: Path = Path(*CODEX_HOME_RELATIVE_PATH)

# The service tier codex's fast toggle maps onto; ``None`` clears the tier (back to the default,
# non-fast tier). The ledger's mirror reads it back as ``fast = serviceTier == "priority"``.
_FAST_SERVICE_TIER: str = "priority"

# Cosmetic ``clientInfo`` for the short-lived switch connection's ``initialize`` handshake.
_SWITCH_CLIENT_NAME: str = "minds-system-interface"
_SWITCH_CLIENT_VERSION: str = "0"


def _read_persisted_root_thread_id(agent_state_dir: Path) -> str | None:
    """The agent's persisted root app-server thread id (written by the plugin), or None."""
    try:
        content = (agent_state_dir / APP_SERVER_THREAD_FILENAME).read_text(encoding="utf-8")
    except OSError:
        return None
    stripped = content.strip()
    return stripped or None


class _RootThreadClient(Protocol):
    """The slice of :class:`CodexAppServerClient` :func:`_bind_root_thread` needs. A Protocol so
    tests inject a plain fake; the real client satisfies it structurally."""

    def thread_loaded_list(self) -> tuple[str, ...]: ...

    def bind_thread(self, thread_id: str) -> None: ...

    def thread_resume(self, thread_id: str) -> Any: ...


def _bind_root_thread(client: _RootThreadClient, agent_state_dir: Path) -> None:
    """Bind the agent's root thread on a fresh switch connection, or raise if none is resolvable.

    Prefers the persisted root id (bound directly when the daemon still holds it loaded, else
    reloaded from its on-disk rollout via ``thread/resume``); with no persisted id, adopts the
    single loaded thread. Mirrors the plugin's send-path binding: a settings update needs a bound
    thread, and a status/settings connection must never start a fresh conversation.
    """
    persisted_thread_id = _read_persisted_root_thread_id(agent_state_dir)
    loaded_thread_ids = client.thread_loaded_list()
    if persisted_thread_id is not None and persisted_thread_id in loaded_thread_ids:
        client.bind_thread(persisted_thread_id)
        return
    if persisted_thread_id is not None:
        client.thread_resume(persisted_thread_id)
        return
    if len(loaded_thread_ids) == 1:
        client.bind_thread(loaded_thread_ids[0])
        return
    raise CodexAppServerError("no unambiguous codex root thread to bind for a model switch")


def _subscribe_root_thread(client: _RootThreadClient, agent_state_dir: Path) -> None:
    """Resume the agent's root thread so this connection JOINS the app-server event stream.

    The live-connection analogue of :func:`_bind_root_thread`, and the linchpin of the codex
    message lifecycle. Where the switch path binds an already-loaded thread LOCALLY
    (``bind_thread``, no RPC), the persistent ledger connection MUST load the root thread via
    ``thread/resume`` -- the only load the app-server treats as a subscription. A bound connection
    is never subscribed, so it hears only ``thread/status/changed`` and never the thread's
    ``turn/*`` / ``item/*`` notifications (delivery and reconcile); resuming is what makes those
    events flow. The resume is safe and additive (verified live against codex 0.147): it replays no
    history (``initialTurnsPage`` is null -- history is pulled via ``thread/read``, not pushed) and
    never perturbs or steals the stream from the concurrent ``--remote`` TUI.

    Prefers the persisted root id; with none, adopts the single loaded thread. Unlike the bind
    path there is no "already loaded -> skip the RPC" shortcut: even a loaded thread is resumed,
    because only the resume subscribes. Raises if no unambiguous root thread is resolvable.
    """
    persisted_thread_id = _read_persisted_root_thread_id(agent_state_dir)
    if persisted_thread_id is not None:
        client.thread_resume(persisted_thread_id)
        return
    loaded_thread_ids = client.thread_loaded_list()
    if len(loaded_thread_ids) == 1:
        client.thread_resume(loaded_thread_ids[0])
        return
    raise CodexAppServerError("no unambiguous codex root thread to subscribe for the live connection")


def open_bound_codex_client(agent_state_dir: Path) -> CodexAppServerClient:
    """Open a short-lived, handshaken, root-thread-bound client to the agent's daemon socket.

    The switch analogue of the plugin's per-send connection: connect the daemon's unix socket,
    ``initialize``, and bind the root thread. The caller ``close()``s it. Raises
    :class:`CodexAppServerError` (or a transport ``OSError``) when the daemon is unreachable or no
    root thread can be bound. This is the MODEL-SWITCH opener: it only needs a bound thread for a
    settings write and deliberately does NOT subscribe to the event firehose -- the live connection
    uses :func:`open_subscribed_codex_client` instead.
    """
    socket_path = get_codex_app_server_socket_path(get_codex_home(agent_state_dir))
    transport = connect_app_server_transport(socket_path)
    client = CodexAppServerClient(transport=transport)
    try:
        client.initialize(_SWITCH_CLIENT_NAME, _SWITCH_CLIENT_VERSION)
        _bind_root_thread(client, agent_state_dir)
    except (CodexAppServerError, OSError):
        client.close()
        raise
    return client


def open_subscribed_codex_client(agent_state_dir: Path) -> CodexAppServerClient:
    """Open a handshaken client whose root thread is RESUMED, so it is subscribed to the event stream.

    The live-connection opener (:class:`~imbue.system_interface.harnesses.codex.live_connection.CodexLiveConnection`).
    Same connect + ``initialize`` as :func:`open_bound_codex_client`, but it loads the root thread
    via ``thread/resume`` (:func:`_subscribe_root_thread`) instead of ``bind_thread``, so the ledger
    reading this connection actually hears the thread's ``turn/*`` / ``item/*`` notifications
    (``item/completed`` = delivery, ``turn/completed`` = reconcile) rather than only
    ``thread/status/changed``. The caller ``close()``s it. Raises :class:`CodexAppServerError` (or a
    transport ``OSError``) when the daemon is unreachable or no root thread can be resolved.
    """
    socket_path = get_codex_app_server_socket_path(get_codex_home(agent_state_dir))
    transport = connect_app_server_transport(socket_path)
    client = CodexAppServerClient(transport=transport)
    try:
        client.initialize(_SWITCH_CLIENT_NAME, _SWITCH_CLIENT_VERSION)
        _subscribe_root_thread(client, agent_state_dir)
    except (CodexAppServerError, OSError):
        client.close()
        raise
    return client


class CodexModelResolver(HarnessModelResolver):
    """Switches a codex agent's selection via ``thread/settings/update`` (the live read is shared)."""

    _agent_state_dir: Path
    _open_client: Callable[[], CodexAppServerClient]

    @classmethod
    def build(
        cls,
        agent_info: AgentInfo,
        open_client: Callable[[], CodexAppServerClient] | None = None,
    ) -> "CodexModelResolver":
        self = cls.__new__(cls)
        self._agent_state_dir = agent_info.agent_state_dir
        # ``open_client`` is injected in tests (a scripted client); production opens a short-lived
        # bound connection to the agent's daemon.
        self._open_client = (
            open_client if open_client is not None else lambda: open_bound_codex_client(agent_info.agent_state_dir)
        )
        return self

    def switch(self, identity: ModelIdentity, axes: frozenset[ModelAxis], send: Callable[[str], bool]) -> SwitchResult:
        # Codex applies the change over the app-server, NOT by typing into the pane, so ``send`` is
        # unused. Each axis maps to a ``thread/settings/update`` field; only the axes the click
        # changed are included (an omitted field leaves that setting unchanged -- the shared switch
        # contract). Fast maps onto the ``priority`` service tier (cleared to default when off).
        settings: dict[str, Any] = {}
        if ModelAxis.MODEL in axes:
            settings["model"] = identity.model_id
        if ModelAxis.EFFORT in axes:
            settings["effort"] = identity.effort
        if ModelAxis.FAST in axes:
            settings["service_tier"] = _FAST_SERVICE_TIER if identity.fast else None
        if not settings:
            return SwitchResult(ok=True)
        client = self._open_client()
        try:
            client.settings_update(**settings)
        except CodexAppServerError as exc:
            logger.warning("codex switch: thread/settings/update failed: {}", exc)
            # A genuine transport/daemon failure. An unavailable MODEL is NOT this path (the daemon
            # falls back and echoes the effective value instead of erroring), so there is no
            # model-failure popup -- only an actual failure to reach the daemon surfaces here.
            return SwitchResult(ok=False, detail="Failed to apply the model change")
        finally:
            client.close()
        # EAGER_THEN_RECONCILE: the chip moved optimistically on click; the daemon's
        # thread/settings/updated echo, mirrored to minds_model_state.json, reconciles it.
        return SwitchResult(ok=True)
