"""Codex's model catalog and its model resolver.

Codex drives the stock ``codex app-server``: a model/effort/fast change is
``thread/settings/update`` over the per-agent daemon, and the daemon echoes the
effective settings back on ``thread/settings/updated``. The live model chip is
kept uniform: the ledger (:mod:`~imbue.system_interface.harnesses.codex.ledger`)
mirrors each ``thread/settings/updated`` to the agent's
``model_state.json`` -- ``{"model", "effort", "fast"}``, the uniform
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
no slots at all.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from typing import Protocol

from loguru import logger
from pydantic import ValidationError

from imbue.mngr.utils.file_utils import atomic_write
from imbue.mngr.utils.file_utils import read_json_dict
from imbue.mngr_codex.app_server_client import CodexAppServerClient
from imbue.mngr_codex.app_server_client import CodexAppServerError
from imbue.mngr_codex.app_server_client import CodexModel
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

# Codex has NO static catalog: its model set, each model's efforts, and its fast support are all
# per-account and per-model (subscription-tier dependent), sourced live from the daemon's
# ``model/list`` (see :func:`codex_model_to_option`). The harness-level catalog therefore carries
# an EMPTY options tuple -- the per-agent options are computed on demand -- plus the config the bar
# still needs (switch mode, dynamic picker, credit label, atomic-tap capability).
CODEX_CATALOG: HarnessCatalog = HarnessCatalog(
    # Empty by design: the picker is DYNAMIC (per-agent options fetched from model-options), and the
    # chip-match is against the per-agent set (see AgentManager._model_options_for), not this.
    options=(),
    # ON_CHANGE: switch() applies thread/settings/update over the app-server and the daemon echoes
    # thread/settings/updated, which the ledger mirrors to model_state.json in well under a
    # second; the shared reader reconciles the chip. That round-trip is fast enough that the chip
    # moves on the CONFIRMED, pushed change (no optimistic overlay). A model the account cannot use
    # is not an RPC error -- the daemon falls back and echoes the effective value, which the mirror
    # reconciles -- so a failed switch is a silent no-op (no popup).
    switch_mode=SwitchMode.ON_CHANGE,
    # DYNAMIC: the picker's options are per-agent (from model/list), re-fetched on every open.
    picker_mode=PickerMode.DYNAMIC,
    powered_by_text="Powered by Codex",
    # Codex's patched binary watches shoulder_tap_atomic.jsonl and merges parked steer
    # messages into the live turn (ABA-gated on the turn id), so the "Shoulder tap" button
    # can flush atomically without a restart. Only codex supports this today.
    native_atomic_shoulder_tap_possible=True,
)


def codex_model_to_option(model: CodexModel) -> ModelOption:
    """Map one ``model/list`` entry to a catalog :class:`ModelOption` (the one pure mapper).

    The single translation from the daemon's per-account model shape to the harness-neutral option
    the chip-match, the picker, and switch-validation all agree on:

    * ``id`` = ``model`` (the id ``thread/settings/update`` switches to, and the id the live state
      file reports -- so no ``harness_reported_model_id`` is needed);
    * ``label`` = ``display_name``;
    * ``efforts`` = ``supported_reasoning_efforts`` verbatim, per-model (no static uniform set);
    * ``supports_fast`` = whether the model offers the ``priority`` service tier;
    * ``in_picker`` = ``not hidden`` (a hidden model is still matchable if the live state reports it,
      but never offered -- mirroring claude's ``ultra``).
    """
    return ModelOption(
        id=model.model,
        label=model.display_name,
        efforts=tuple(EffortChoice(level=effort.reasoning_effort) for effort in model.supported_reasoning_efforts),
        supports_fast=any(tier.id == FAST_SERVICE_TIER for tier in model.service_tiers),
        in_picker=not model.hidden,
    )


def codex_models_to_options(models: tuple[CodexModel, ...]) -> tuple[ModelOption, ...]:
    """Map a whole ``model/list`` result to the per-agent catalog options (in daemon order)."""
    return tuple(codex_model_to_option(model) for model in models)


# The codex-scoped sidecar that persists the RAW ``model/list`` result across a restart, so the
# per-agent option set -- and thus the model chip -- resolves BEFORE the daemon reconnects. It sits
# beside ``model_state.json`` under CODEX_HOME. RAW ``CodexModel`` entries are stored (NOT
# mapped ``ModelOption``s); :func:`codex_models_to_options` maps them on READ. This keeps the
# preserve-and-surface contract: the raw daemon source is retained and the derived options are
# recomputed from it, so a later change to the mapping needs no refetch.
CODEX_MODEL_OPTIONS_FILENAME: str = "minds_codex_model_options.json"


def get_codex_model_options_path(agent_state_dir: Path) -> Path:
    """The agent's model-options sidecar: ``<CODEX_HOME>/minds_codex_model_options.json``."""
    return get_codex_home(agent_state_dir) / CODEX_MODEL_OPTIONS_FILENAME


def write_codex_model_options(model_options_path: Path, models: tuple[CodexModel, ...]) -> None:
    """Persist the RAW ``model/list`` result to the sidecar (raw ``CodexModel`` entries, by alias).

    The write-through called on every successful NON-EMPTY live fetch -- the connect-time seed and
    each picker-open fetch -- so the per-agent option set survives a restart and the chip resolves
    offline. An empty list is the caller's signal of a failed/absent fetch and must NOT reach here:
    it would clobber a good sidecar (callers guard on ``if models``). A write failure is logged,
    never raised -- a stale sidecar is preferable to breaking the caller (the connect path / the
    picker endpoint), mirroring :func:`write_codex_model_state`."""
    payload = {"models": [model.model_dump(by_alias=True) for model in models]}
    try:
        atomic_write(model_options_path, json.dumps(payload))
    except OSError as exc:
        logger.opt(exception=exc).warning("codex: failed to write model options to {}", model_options_path)


def read_codex_model_options(model_options_path: Path) -> tuple[CodexModel, ...]:
    """Load the persisted RAW ``model/list`` entries from the sidecar, or ``()`` if absent/malformed.

    The lazy-load fallback SOURCE: when the in-memory per-agent option set is empty (post-restart,
    before the daemon reconnects), the chip-match reads here and maps via
    :func:`codex_models_to_options`. A missing/unparseable file yields ``()`` (the chip then falls
    back to rendering no slots), and any single entry that no longer matches the ``CodexModel`` shape is
    skipped rather than sinking the whole file -- so a daemon schema drift degrades gracefully."""
    raw_models = read_json_dict(model_options_path).get("models")
    if not isinstance(raw_models, list):
        return ()
    models: list[CodexModel] = []
    for entry in raw_models:
        if not isinstance(entry, dict):
            continue
        try:
            models.append(CodexModel.model_validate(entry))
        except ValidationError as exc:
            logger.debug("codex: skipping a malformed model-options entry in {} ({})", model_options_path, exc)
    return tuple(models)


# Codex writes its live state under CODEX_HOME (``<state_dir>/plugin/codex/home``), not at
# the state-dir root, so the shared reader/watch path takes this relative directory as data.
CODEX_STATE_RELATIVE_PATH: Path = Path(*CODEX_HOME_RELATIVE_PATH)

# The service tier codex's fast toggle maps onto; ``None`` clears the tier (back to the default,
# non-fast tier). The ledger's mirror reads it back as ``fast = serviceTier == "priority"``. Public
# so the live connection can seed the same fast<->tier mapping from the resume ThreadInfo (§8).
FAST_SERVICE_TIER: str = "priority"

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

    def list_offered_options(self) -> tuple[ModelOption, ...] | None:
        """The per-agent picker options, fetched FRESH from ``model/list`` on every open (D2).

        Codex's picker is DYNAMIC: its options are account/daemon-derived, not a static catalog, so
        this opens a short-lived bound connection (the switch opener), reads ``model/list``, and maps
        each entry to a :class:`ModelOption`. Always fresh, so a subscription-tier change shows up
        without a restart. A daemon/transport failure yields an empty tuple (the picker shows no
        models rather than a stale list); the chip still renders from the pushed live choice.
        """
        try:
            client = self._open_client()
        except (CodexAppServerError, OSError) as exc:
            logger.debug("codex model-options: daemon not reachable ({})", exc)
            return ()
        try:
            models = client.model_list()
        except (CodexAppServerError, OSError) as exc:
            logger.debug("codex model-options: model/list failed ({})", exc)
            return ()
        finally:
            client.close()
        # Write-through (picker-open path): persist this fresh, non-empty raw list to the sidecar so
        # the chip resolves offline after a restart. Only a non-empty fetch is persisted -- a failed
        # fetch already returned above, so it never clobbers a good sidecar.
        if models:
            write_codex_model_options(get_codex_model_options_path(self._agent_state_dir), models)
        return codex_models_to_options(models)

    def switch(self, identity: ModelIdentity, axes: frozenset[ModelAxis], send: Callable[[str], bool]) -> SwitchResult:
        # Codex applies the change over the app-server, NOT by typing into the pane, so ``send`` is
        # unused. Each axis maps to a ``thread/settings/update`` field. Fast maps onto the
        # ``priority`` service tier (cleared to default when off).
        #
        # A MODEL-axis change (re)asserts ALL THREE axes together, not just the diffed ones: the
        # daemon does NOT enforce a model's ``service_tiers`` (verified live -- switching to a
        # no-priority model while ``serviceTier`` is still ``priority`` KEEPS priority), so a stale
        # ``priority`` would survive a model switch unless we clear it explicitly. Re-sending the
        # new model's effort likewise lands it valid. So effort/service_tier ride along whenever the
        # model axis changes OR their own axis did; a pure effort/fast click sends only its own axis,
        # leaving the others unchanged (the shared switch contract).
        model_changed = ModelAxis.MODEL in axes
        settings: dict[str, Any] = {}
        if model_changed:
            settings["model"] = identity.model_id
        if model_changed or ModelAxis.EFFORT in axes:
            settings["effort"] = identity.effort
        if model_changed or ModelAxis.FAST in axes:
            settings["service_tier"] = FAST_SERVICE_TIER if identity.fast else None
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
        # ON_CHANGE: the daemon's thread/settings/updated echo, mirrored to model_state.json,
        # is pushed back as the authoritative choice, moving the chip on the confirmed change.
        return SwitchResult(ok=True)
