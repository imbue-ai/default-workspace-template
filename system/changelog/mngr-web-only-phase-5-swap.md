The in-container owner-exec daemon is removed. Only the hosted web client that Imbue Studio's web build replaces sent it requests; that build calls the workspace's own routes (`/api/workspace/...`).

- Removed: `system/supervisord.conf.d/owner-exec.conf`, the launcher `system/scripts/run_owner_exec.sh`, the installer `system/scripts/install_owner_exec.sh` with its version pin, and their lines in `system/Dockerfile`, `system/scripts/setup_system.sh` and the offload configs.

- Removed: `system/scripts/provision_backups.py` and its module `workspace_bare_scripts/provision_backups.py`, which only that client's create ran. Backups are set up through the workspace's backup routes.

- `setup_system.sh` deletes `/usr/local/bin/owner-exec`, its version stamp and its installer from a machine an earlier release provisioned, so a workspace that updates keeps no binary nothing runs.

- The design docs and specs under `docs/system/` no longer describe owner-exec or `provision_backups` as part of a workspace.
