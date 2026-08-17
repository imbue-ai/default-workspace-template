`with_agent_env.sh` moved from `system/libs/automations/` up to `system/scripts/`.

The wrapper rebuilds the agent environment from the files mngr maintains, for callers that run without it. That was cron when it was written, which is why it lived with the automations machinery -- but sshd scrubs the environment the same way, so a command sent over SSH needs it just as much. It is shared infrastructure rather than an automations detail, and now sits with the rest of `system/scripts/`.

Cron entries live in `data/.state/cron.d/`, which is gitignored and survives `update-self`, so any job scheduled before the move still names the old path. A shim remains at `system/libs/automations/with_agent_env.sh` and execs the new location, so those jobs keep running; it is marked for cleanup once no workspace refers to it.
