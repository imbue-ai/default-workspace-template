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

2. **`runtime-backup` can be permanently wedged by a stale git lock.** If stop
   SIGKILLs it mid-`git commit`, a stale `index.lock` is left in the runtime
   worktree. The service never clears stale locks, so every subsequent tick
   fails identically and backups stop silently and forever.

3. **`system_interface` has no `restart` policy** while every other
   long-running server has `restart = "on-failure"` -- an inconsistency
   (moot today because of #1).

4. **`app-watcher` re-emits `service_registered` events on every restart**
   (its diff state resets to empty), appending duplicates to the persistent
   `events.jsonl`; it also cannot emit `service_deregistered` for services
   removed during downtime.

5. **Stopping `system-services` silently degrades every other agent.**
   chat/worker agents keep running headless but lose the UI, web view,
   tunnel, and -- critically -- `runtime-backup`. Container loss during the
   stop window loses all runtime state since the last pre-stop backup tick.

6. **Minor:** an interrupted one-shot `deferred-install` re-runs cleanly on
   start. A very narrow first-boot-only window can strand prior `runtime/`
   content in `runtime.preexisting/` if bootstrap is killed mid-init.

## Conclusion

A deliberate stop/start is safe. The real exposure is unplanned failure:
nothing restarts a crashed service (#1), and `runtime-backup` has a silent
permanent-failure mode (#2).
