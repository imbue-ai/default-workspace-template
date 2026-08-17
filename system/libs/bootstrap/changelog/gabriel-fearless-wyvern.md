The bootstrap no longer writes anything to `/etc/environment`.

An earlier revision of this branch published the services agent's identity variables there at each boot, so that SSH sessions would see the same `MNGR_*` environment an agent shell does -- the motivating failure being that `host-backup-now` over SSH could not find its events log, since both of its resolution paths need `MNGR_HOST_DIR` or `MNGR_AGENT_STATE_DIR`.

Publishing to every session turned out to be the wrong shape for that. `/etc/environment` is world-readable and reaches every SSH session whether or not it wanted the agent environment, and one of the published variables was `MNGR_AGENT_ID` -- which mngr's teardown scans `/proc` for when hunting an agent's orphaned processes. An idle SSH session holding it would have been indistinguishable from an orphan and killed on any `mngr stop`, including one holding the very backup the publishing existed to enable. Avoiding that would have meant splitting mngr's teardown onto a second, private marker: a change to core process-lifecycle state, paid for a convenience.

`system/scripts/with_agent_env.sh` already solved the same problem for cron, which scrubs the environment exactly as sshd does. Callers that need the agent environment over SSH now prefix their command with it, so only the command that asked for the environment gets it and nothing global changes.
