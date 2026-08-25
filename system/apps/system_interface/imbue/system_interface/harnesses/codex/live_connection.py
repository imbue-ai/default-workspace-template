"""One live app-server connection per codex agent -- the ledger's persistent home.

The message-lifecycle contract (see ``docs/design/harness-message-lifecycle-contract.md``)
makes the backend the sole authority for a codex agent's five message states, and
:class:`~imbue.system_interface.harnesses.codex.ledger.CodexMessageLedger` is that authority.
The ledger is a pure reducer over the stock ``codex app-server`` notification stream, so it needs
exactly one long-lived, thread-bound
:class:`~imbue.mngr_codex.app_server_client.CodexAppServerClient` feeding it. This class owns that
connection for one agent:

* it opens + handshakes + RESUMES the agent's root thread
  (:func:`~imbue.system_interface.harnesses.codex.model.open_subscribed_codex_client`) -- the
  ``thread/resume`` is what SUBSCRIBES the connection to the thread's ``turn/*`` / ``item/*``
  notifications, so the ledger hears delivery (``item/completed``) and reconcile
  (``turn/completed``); a ``bind_thread`` (the switch path) is local-only and would leave the
  ledger deaf to everything but ``thread/status/changed``. It then seeds the client's
  ``active_turn_id`` from the live ``thread/status`` (one ``thread/read``) so the very first send
  parks vs starts correctly and a mid-turn reconnect starts from the in-progress turn;
* it builds the ledger over that client with the manager's queue/activity/user-turn/model
  callbacks (``on_user_turn`` broadcasts each committed user-turn to the transcript stream -- the
  ledger owns live user-turns, the rollout file reader suppresses them, Fix 1);
* it runs a background reader thread that pumps ``poll_notifications`` into the ledger.

The send / interrupt / shoulder-tap endpoints reach the ledger synchronously (through the agent
manager) on their own request threads while the reader is polling; the client's frame lock
serializes the two, so the reader and a live send never steal each other's frames.

The connection is EPHEMERAL (contract): it lives with the agent's daemon generation. When the
daemon dies the reader observes a closed transport and marks the connection not-alive; the agent
manager reaps it and builds a fresh one on the next observe tick, whose ledger starts with an
empty queue -- nothing from the dead generation is revived.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from loguru import logger

from imbue.mngr_codex.app_server_client import CodexAppServerClient
from imbue.mngr_codex.app_server_client import CodexAppServerError
from imbue.mngr_codex.app_server_client import CodexModel
from imbue.mngr_codex.app_server_client import TransportClosedError
from imbue.system_interface.harnesses.codex.ledger import CodexMessageLedger
from imbue.system_interface.harnesses.codex.ledger import write_codex_model_state
from imbue.system_interface.harnesses.codex.model import FAST_SERVICE_TIER
from imbue.system_interface.harnesses.codex.model import open_subscribed_codex_client

# How long the background reader blocks on one ``poll_notifications`` drain before looping. It
# holds the client's frame lock for that span when the stream is idle, so a live send waits at
# most this long for the lock -- short enough to feel immediate (contract A5), long enough to
# avoid a hot spin. During an active turn the reader drains available frames and releases at once.
_READER_POLL_TIMEOUT_SECONDS: float = 0.2

# How long ``stop`` waits for the reader thread to unwind before closing the client under it.
_READER_JOIN_TIMEOUT_SECONDS: float = 3.0


def _fetch_codex_models(client: CodexAppServerClient, agent_state_dir: Path) -> tuple[CodexModel, ...]:
    """Read the account's ``model/list`` for the chip-match cache, tolerating a daemon/transport miss.

    A failure returns an empty tuple rather than raising -- the connection is still fully usable for
    messages; the chip simply falls back to whatever the live state file already reports."""
    try:
        return client.model_list()
    except (CodexAppServerError, OSError) as exc:
        logger.debug("codex live connection: model/list failed for {} ({})", agent_state_dir, exc)
        return ()


def _seed_model_state_from_resume(client: CodexAppServerClient, model_state_path: Path) -> None:
    """Seed the model-state file from the settings the opener's ``thread/resume`` reported (§8).

    On connect the daemon has NOT emitted a ``thread/settings/updated``, so the ledger's mirror has
    nothing to write yet; the durable file could be stale or absent. The ``thread/resume`` response
    carried the thread's live ``model`` / ``effort`` / ``serviceTier``, captured on the client as
    ``last_thread_info`` -- write it so the chip matches the daemon on connect. No info (a bound test
    client that never resumed) or no model is a no-op."""
    info = client.last_thread_info
    if info is None or not info.model:
        return
    write_codex_model_state(model_state_path, info.model, info.effort, info.service_tier == FAST_SERVICE_TIER)


class CodexLiveConnection:
    """The persistent client + ledger + reader thread for one codex agent's daemon generation."""

    _client: CodexAppServerClient
    _ledger: CodexMessageLedger
    _stop_event: threading.Event
    _reader_thread: threading.Thread
    # Flipped False when the reader observes a closed/failed transport (the daemon died). The
    # agent manager treats a not-alive connection as absent and rebuilds.
    _is_alive: bool
    # The account's models from ``model/list``, fetched once on connect and cached for the whole
    # daemon generation. This is the per-agent model set the chip-match reads (via AgentManager) --
    # no daemon call in the hot recompute path. The picker re-fetches fresh per open (D2); this
    # cache only backs the chip-match. Empty when the fetch failed (the chip still renders logo-only
    # or falls back to whatever the live state file already holds).
    _codex_models: tuple[CodexModel, ...]

    @classmethod
    def build(
        cls,
        agent_state_dir: Path,
        *,
        on_queue_snapshot: Callable[[list[dict[str, Any]]], None],
        on_user_turn: Callable[[dict[str, Any]], None],
        model_state_path: Path,
        open_client: Callable[[Path], CodexAppServerClient] = open_subscribed_codex_client,
    ) -> "CodexLiveConnection | None":
        """Open the connection and start pumping, or ``None`` if the daemon is not reachable yet.

        A ``None`` return is the normal not-ready case (the socket is absent or the daemon is
        still starting): the caller logs at debug and retries on the next observe tick. Only a
        reachable daemon yields a live connection. ``open_client`` is the
        connect+handshake+resume step -- the resume is what subscribes this connection to the event
        stream (a test seam; production connects to the agent's daemon socket).
        """
        try:
            client = open_client(agent_state_dir)
        except (CodexAppServerError, OSError) as exc:
            logger.debug("codex live connection: daemon not reachable for {} ({})", agent_state_dir, exc)
            return None
        try:
            # Seed active_turn_id from the live thread status so the first send parks (busy) vs
            # starts (idle) correctly on this fresh connection, exactly as the plugin's send does.
            client.read_thread_status()
        except (CodexAppServerError, OSError) as exc:
            logger.debug("codex live connection: status read failed for {} ({}); closing", agent_state_dir, exc)
            client.close()
            return None
        # Cache the account's model set once for this daemon generation -- the per-agent set the
        # chip-match reads. A failure yields an empty cache (the chip falls back to the live file).
        codex_models = _fetch_codex_models(client, agent_state_dir)
        # Seed the durable model-state file from the settings the daemon RESUMED with (§8), so the
        # chip matches the daemon on connect even before any thread/settings/updated fires. The
        # opener captured this ThreadInfo on its thread/resume; None (e.g. a bound test client) skips.
        _seed_model_state_from_resume(client, model_state_path)
        ledger = CodexMessageLedger.build(
            client,
            on_queue_snapshot=on_queue_snapshot,
            on_user_turn=on_user_turn,
            model_state_path=model_state_path,
        )
        self = cls.__new__(cls)
        self._client = client
        self._ledger = ledger
        self._codex_models = codex_models
        self._stop_event = threading.Event()
        self._is_alive = True
        self._reader_thread = threading.Thread(target=self._read_loop, name="codex-ledger-reader", daemon=True)
        self._reader_thread.start()
        return self

    @property
    def ledger(self) -> CodexMessageLedger:
        """The ledger over this connection -- the backend authority for the agent's messages."""
        return self._ledger

    @property
    def client(self) -> CodexAppServerClient:
        """The persistent client this connection owns."""
        return self._client

    @property
    def codex_models(self) -> tuple[CodexModel, ...]:
        """The account's models (from ``model/list``), cached on connect -- the per-agent set the
        chip-match reads. Empty when the connect-time fetch failed."""
        return self._codex_models

    @property
    def is_alive(self) -> bool:
        """Whether the daemon connection is still up (reader running, transport not closed)."""
        return self._is_alive and self._reader_thread.is_alive()

    def _read_loop(self) -> None:
        """Pump notifications into the ledger until stopped or the transport closes.

        Every dispatched frame runs through the client's registered handler
        (``ledger.handle_notification``), which is what advances the message states and fires the
        queue/activity callbacks. A closed or failed transport ends the loop and marks the
        connection not-alive so the manager rebuilds a fresh (empty-queue) connection.
        """
        while not self._stop_event.is_set():
            try:
                self._client.poll_notifications(timeout=_READER_POLL_TIMEOUT_SECONDS)
            except TransportClosedError:
                logger.info("codex live connection: transport closed; connection is done")
                self._is_alive = False
                return
            except (CodexAppServerError, OSError) as exc:
                logger.info("codex live connection: reader failed ({}); connection is done", exc)
                self._is_alive = False
                return

    def stop(self) -> None:
        """Stop the reader and close the client. Idempotent."""
        self._stop_event.set()
        self._reader_thread.join(timeout=_READER_JOIN_TIMEOUT_SECONDS)
        try:
            self._client.close()
        except (CodexAppServerError, OSError) as exc:
            logger.debug("codex live connection: error closing client ({})", exc)
