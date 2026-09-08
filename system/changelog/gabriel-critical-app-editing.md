`system/supervisord.conf` gains the `agent-observer` program: the workspace's one `mngr observe --quiet`, run from the primary agent's work dir in the chat's memory band, restarted on exit like every service. The chat app no longer runs an observer of its own; every chat instance follows the event file this program writes. `supervisorctl restart agent-observer` bounces it, and a chat rides the outage out (its `/api/health` reports it) rather than freezing.

`system/vendor/mngr` is refreshed from the paired mngr branch `gabriel/critical-app-editing`, which carries the observe read side (`ObserveEventFollower`, `is_observe_writer_running`, `find_last_full_state_offset`), the follower's start-without-a-writer mode the chat relies on, and the `initial_branch` widening.

The workspace app model's contracts record the chat's `agent_events` health field and the observer's band; the chat, apps, and services READMEs describe the program.

`system/test_app_manifests.py` checks that every critical built-in's `[preview]` table names the app's own console script, and pins the chat, shell, and terminal tables' shapes.

The workspace app model's contracts describe the manifest's `[preview]` table and the built-ins' tables, the preview shell's 403 on the relay verbs and its meta tag, `is_preview` on the inventory document, and where an isolated instance's copies and scratch space live.
