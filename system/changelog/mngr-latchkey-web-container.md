- The root `pyproject.toml` names `system/libs/auth_sandbox` under `[tool.uv.workspace] exclude`: the auth sandbox library is its own uv project with its own lock, which only the auth sandbox image installs, so a workspace's venv never carries its `imbue-mngr-latchkey` and `imbue-mngr-forward` (whose mngr plugins `system/config/mngr_plugins.toml` does not assign the workspace's mngr). `user_packages.py` treats an excluded package as built-in rather than a user package, so no workspace build or provisioning syncs it; the root suite skips it, and CI runs its suite from its own venv against the pinned latchkey CLI.

- `system/scripts/set_mngr_pin.py` moves the mngr pin in an excluded package's `pyproject.toml` together with the root's, and `--check` refuses a tree where the two differ.

- `system/scripts/register_vm_exec.sh` reads the container's default gateway from `/proc/net/route` when `ip` is absent, which it is in the product image: until now the script skipped registration on every slice ("no 'ip' command"), so no imbue-cloud workspace ever listed `vm-exec` in its service map and the web chrome could not provision the VM's latchkey gateway.

- `system/scripts/run_owner_exec.sh` takes owner-exec's listen address from `OWNER_EXEC_LISTEN_HOST` when it is set (`127.0.0.1` otherwise, as before); the web latchkey auth sandbox's boot script sets `0.0.0.0` so Modal's encrypted tunnel reaches the daemon.

- `system/scripts/setup_system.sh` installs latchkey 3.21.0 (was 3.19.4), the version the VM gateway package installs and the floor `imbue-mngr-latchkey` enforces on every run, so the auth sandbox image (which installs through this script) can run the sign-in at all.

- `system/scripts/install_owner_exec.sh` installs owner-exec v0.2.3 (was v0.2.2), which exposes its response-signature headers to the web chrome origin so the chrome can verify a VM's vm-exec answers. The monorepo's VM-install pin moves in lockstep.
