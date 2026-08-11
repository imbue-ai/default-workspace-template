# Codex app-server — integration + live-validation runbook

Status: READY-TO-EXECUTE (2026-08-11). The remaining work after the component build. The components
(lifecycle off `thread/status`, `CodexMessageLedger` + ephemeral queue, interrupt/tap, the model
writer, the conservation gate) are built, unit-tested, and committed on `main` (also preserved on
branch `codex-appserver-components` @ `1870327408`). What is NOT done is **wiring them into the live
path + deleting the fork-era machinery they replace**. This IS validatable HERE: the
`codex app-server` daemon runs turns fine under gVisor once codex's OWN sandbox is disabled
(`-c sandbox_mode=danger-full-access`, as Minds agents already use; bubblewrap uninstalled — it was
a false blocker). A full turn was proven end-to-end (the model replied `pong`; delivery=commit
verified). This runbook is the exact sequence to finish + live-validate it.

## 0. Running a real daemon here (PROVEN)

The daemon runs turns under gVisor with codex's own sandbox disabled — no bubblewrap:
- `H="$(mktemp -d)"; cp /tmp/codex147-home/auth.json "$H"/` (the logged-in API key), then
  `CODEX_HOME="$H" codex app-server --listen unix://<UNIQUE.sock> -c sandbox_mode=danger-full-access`.
- Drive it with the committed `CodexAppServerClient`; `/tmp/e2e.py` is a working template that runs a
  full turn (userMessage commits, `turn/completed`, the model replies). Use a UNIQUE home + socket
  per concurrent driver.

## 1. Wire the per-agent persistent connection (the load-bearing missing piece)

Today `CodexMessageLedger` is instantiated only in tests. Give each codex agent one live connection:
- In the system_interface app context / `AgentManager` per-agent setup, for a codex agent:
  instantiate `CodexMessageLedger.build(...)`, open a WS connection to the agent's socket
  (`get_codex_app_server_socket_path(codex_home)`), run a **background reader thread** that pumps
  notifications into the ledger, and handle reconnect (socket reappears after a daemon restart →
  new generation → the ephemeral queue starts empty per contract).
- Reader-vs-send concurrency: the send path (short-lived `submit`) and the persistent reader both
  touch the client — guard with the client's own lock; this is the concurrency the sandbox could
  not exercise, so it MUST be validated live (§5).

## 2. Route the live endpoints through the ledger, THEN delete the fork path (coordinated)

Do each pair atomically (wire the new, delete the old) so no duplicate coexists:
- **Send** — `server.py /api/agents/<id>/message` for codex → the ledger (mint `clientId`, `submit`,
  record). (The plugin `send_message` for the CLI already drives the app-server; keep it.)
- **Shoulder-tap** — `server.py:909` → `ledger.is_tap_available()` + ensure-steered. DELETE the
  `flush_codex_queue_atomic` call + the function.
- **Stop/interrupt** — codex interrupt → `ledger.interrupt()`. DELETE `CodexInterruptToComposer`
  wiring at `registry.py:118` + `execute_codex_stop_to_composer` / `_settle_markers_after_retract` /
  `RETRACT_SETTLE_DEADLINE`.
- **Queue snapshot** — `ledger.queued_snapshot()` → the existing `update_queued_messages`. DELETE
  `CodexQueueTracker` (`watcher.py:215`) + `queue_tracker.py` + the watcher's queued-input tail.
- **Activity dot** — feed it from the ledger's `thread/status` + turn/item stream (RUNNING until
  `turn/completed`, contract A6). DELETE codex's `ACTIVE_MARKER_FILENAME` read at
  `agent_manager.py:1383` and `activity_state.py`'s codex derivation. **This also closes the current
  regression** (Phase 1 deleted the marker writers; this deletes the now-dangling reader).
- **Model chip** — the ledger already writes `minds_model_state.json` from `thread/settings/updated`
  (`ledger.py:295`); once the connection is live, the UNIFORM read path
  (`agent_manager._recompute_model_choice`) picks it up unchanged. Nothing to delete here (the read
  path was correctly left untouched).

## 3. mngr-side cleanup (once nothing imports them)

Delete `mark_codex_agent_idle` + `codex_marker_state.sh` (mngr) once `harnesses/codex/model.py:41`
no longer imports `mark_codex_agent_idle` (it will not, after §2's stop rewiring).

## 4. Known follow-ups to fix in the same pass

- `mngr/.../agent_release_testing.py` observes RUNNING via the raw `active` marker codex no longer
  writes → re-point it at the new `thread/status`-derived lifecycle, or the codex release e2e fails
  its RUNNING assertion.
- DRY: `open_bound_codex_client` / `_bind_root_thread` (dwt short-lived connect) duplicate the
  plugin's connect-and-bind (mngr) across the package boundary — collapse onto the persistent
  connection.

## 5. Live validation (the stake-your-life gate — only meaningful on the real runtime)

Create a real codex agent and confirm, on screen:
1. Send while idle → the message commits and appears (Delivered); dot RUNNING until it lands.
2. Send mid-turn → queued chip → auto-consumed at the next boundary → Delivered; chip removed
   before the turn appears (A3b).
3. Shoulder-tap a queued message → delivered in order.
4. Stop mid-flight with a queue → every non-committed message returns to the composer in send order;
   Delivered ones stay; dot clears immediately.
5. Switch model/effort/fast from the TUI → the chip reconciles from `minds_model_state.json`; and
   from the web bar → `thread/settings/update` moves it.
6. `mngr ls` shows RUNNING while a turn runs, WAITING on an approval, STOPPED when the daemon dies
   (`--remote` exits → process gone).
7. Kill the daemon mid-queue → fresh session starts with an EMPTY queue (ephemeral; nothing revived).
Then: the codex conservation gate + the full `system_interface` suite + the mngr plugin suite, all
green (no coverage-disable flags). Only when §5 passes live is codex stakeable.

## Status

We finish HERE — the daemon runs turns (§0). The current `main` is half-migrated (the ledger is not
yet the live path; the fork-era duplicates + the now-dangling activity-marker read remain); §§1–5
close that. The built components are also preserved on `codex-appserver-components`.
