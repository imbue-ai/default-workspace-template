# Audit: `system-services` agent under stop/start

Scenario examined: an operator runs `mngr stop system-services` followed by
`mngr start system-services`. What runs in the agent, and what is fragile?

## What runs in the `system-services` agent

`system-services` is the mngr `main`-type agent. Its tmux session contains:

- **Window 0**: `sleep infinity && claude` -- deliberately inert (never reaches `claude`).
- **`extra_window` entries** (`.mngr/settings.toml` -> `create_templates.main`):
  `bootstrap`, `telegram` (telegram-bot), `terminal` (ttyd),
  `reviewer_settings` (one-shot), `git_auth_setup` (one-shot).
- **`bootstrap`** reads `services.toml` and spawns one `svc-<name>` window per
  service: `system_interface`, `web`, `cloudflared`, `app-watcher`,
  `runtime-backup`, `deferred-install`.

## Stop/start behavior

`mngr stop` SIGTERMs every PID in the session, SIGKILLs survivors after a 5s
grace, then `tmux kill-session`. The container keeps running
(`idle_mode = "disabled"`), so the filesystem is fully preserved.

`mngr start` recreates the whole session, replaying window 0 and all
`extra_window` entries. `bootstrap` re-runs `main()` and reconciles all six
services from a clean slate.

The happy path is solid: restart idempotency is well-designed
(`_init_runtime_worktree` short-circuits on `runtime/.git`; the initial-chat
signal file is persisted; `forward_port.py` does a locked atomic upsert;
`deferred_install.sh` skips on its persisted marker; `cloudflared` uses a
token-based named tunnel that reconnects to the same hostname).

## Fragilities found

1. **`restart` policy is parsed but never implemented.** `manager.py`
   reads `config.get("restart", "never")` and never uses it. `_start_service`
   runs the command inside a persistent shell window, so a crashed service
   leaves an idle-shell window that still looks "running" to
   `_list_managed_windows`; reconcile only fires on `services.toml` mtime
   changes. `restart = "on-failure"` is therefore dead config -- there is no
   per-service crash recovery.
   *Resolved:* service windows now record their command's exit status into a
   `@svc_exit_status` window option; the manager polls it every interval and
   restarts `on-failure` services that exited non-zero.

2. **`runtime-backup` can be permanently wedged by a stale git lock.** If stop
   SIGKILLs it mid-`git commit`, a stale `index.lock` is left in the runtime
   worktree. The service never clears stale locks, so every subsequent tick
   fails identically and backups stop silently and forever.
   *Resolved:* each tick now clears a stale `index.lock` before `git add`
   (safe because runtime-backup is the worktree's only, sequential, writer).

3. **`system_interface` has no `restart` policy** while every other
   long-running server has `restart = "on-failure"` -- an inconsistency.
   *Resolved:* `system_interface` now declares `restart = "on-failure"`.

4. **`app-watcher` re-emits `service_registered` events on every restart**
   (its diff state resets to empty), appending to the persistent
   `events.jsonl`. *Not a defect:* `events.jsonl` is an append-only event
   log, and a service genuinely does re-register on restart, so re-emission
   is correct; `system_interface` reads `applications.toml` directly rather
   than the event log. The only real gap -- missing a `service_deregistered`
   for a service explicitly removed (`forward_port.py --remove`) *during* the
   stop window -- is a negligible edge. No change made.

5. **Stopping `system-services` silently degrades every other agent.**
   chat/worker agents keep running headless but lose the UI, web view,
   tunnel, and -- critically -- `runtime-backup`. Container loss during the
   stop window loses all runtime state since the last pre-stop backup tick.
   *Not fixed:* this is architectural (one agent owns all shared infra). A
   real fix is a separate design effort (e.g. infra supervised outside the
   agent, or a health signal to dependent agents).

6. **Minor:** an interrupted one-shot `deferred-install` re-runs cleanly on
   start. A very narrow first-boot-only window can strand prior `runtime/`
   content in `runtime.preexisting/` if bootstrap is killed mid-init.
   *Resolved:* `_init_runtime_worktree` now restores `runtime.preexisting/`
   unconditionally, including on the path where the worktree already exists.

## Conclusion

A deliberate stop/start is safe. The concrete fragilities (#1, #2, #3, #6)
are fixed in this change; #4 is not a real defect and #5 is an architectural
limitation tracked for a future redesign.
