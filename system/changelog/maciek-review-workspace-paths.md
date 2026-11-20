- On a host that keeps mngr's host dir somewhere other than `/home/user/.mngr` (a Modal-hosted workspace keeps it at `/mngr`), the System Monitor names chats and attributes their processes to them again: it reads the agent records from `MNGR_HOST_DIR` and recognises agent paths under `/mngr/agents/`.

- The interactive-shell line that sources mngr's host env, written into `.bashrc` at provisioning, names the provisioning run's own host dir rather than always `/home/user/.mngr`.

- The apt hook that refreshes the environment record checks for the host dir in `MNGR_HOST_DIR`, so it no longer depends on a `/home/user/.mngr` directory existing.
