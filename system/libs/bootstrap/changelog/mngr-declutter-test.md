Integration branch combining the workspace-layout trains (`mngr/fix-data-layout`, `mngr/declutter-template`) with `mngr/fix-apt-mirror`; the full per-train details live in this project's sibling entries for those branches.

For this project: the bootstrap execs `system/supervisord.conf`, gates the initial chat on `data/.state/initial_chat_created`, and follows the `/home/user` layout and env-converge boot flow (the package itself moves to `system/libs/bootstrap`).
