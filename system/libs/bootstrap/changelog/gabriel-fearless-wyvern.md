The bootstrap now publishes the services agent's identity variables to `/etc/environment` at each boot, so SSH sessions see the same `MNGR_*` environment an agent shell does.

sshd builds a fresh environment for every session and drops its own, so none of the agent environment reached `ssh <host> '<command>'`. Tools that resolve their state from it failed there while working everywhere else -- `host-backup-now` over SSH could not find its events log, because both of its resolution paths need `MNGR_HOST_DIR` or `MNGR_AGENT_STATE_DIR`.

`/etc/environment` rather than a `/etc/profile.d` drop-in: PAM applies it to every session regardless of shell type, whereas a profile script is sourced only by *login* shells. `ssh <host> '<command>'` is neither login nor interactive, so a profile drop-in appears to work when tested by hand and still misses the case this exists to fix.

Only an explicit allowlist of names is published (`MNGR_HOST_DIR`, `MNGR_AGENT_ID`, `MNGR_AGENT_NAME`, `MNGR_AGENT_STATE_DIR`, `MNGR_AGENT_WORK_DIR`, `MNGR_PRIMARY_WINDOW_NAME`, `LLM_USER_PATH`). The file is world-readable and the agent environment also carries credentials, so an allowlist of names -- rather than a `MNGR_*` prefix match -- is what keeps a secret added later from starting to leak.
