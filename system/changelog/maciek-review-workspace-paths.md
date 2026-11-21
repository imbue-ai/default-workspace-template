- The interactive-shell line that sources mngr's host env, written into `.bashrc` at provisioning, names the provisioning run's own host dir rather than always `/home/user/.mngr`, so a terminal on a Modal-hosted workspace (host dir `/mngr`) gets the agent environment.

- The apt hook that refreshes the environment record looks for the host dir named in `MNGR_HOST_DIR`, falling back to `/home/user/.mngr`.
