# Outer autostart installer: fire the start on install, tolerate scriptless containers

Two fixes to the minds-outer-autostart installer in `.mngr/settings.toml` (all five provider-template copies), found during the 2026-08-13 production pool-box recovery:

- The installer now explicitly fires the workspace start with `systemctl restart --no-block minds-autostart.service`. Starting the path unit alone never re-runs a service the previous installer's boot-time oneshot left latched active (`RemainAfterExit=yes` plus swallowed failures) -- exactly the state of a wedge-recovered VM, which made the fleet backfill sweep a silent no-op there.

- The in-container relaunch step now probes the known per-generation locations of `minds_start_services_agent.sh` and, for containers that predate the script entirely (pre minds-v0.3.2), brings up in-container sshd and succeeds with a journal notice instead of failing -- a permanently failing oneshot retriggered by the unthrottled path unit previously hot-looped at ~5 starts/second.
