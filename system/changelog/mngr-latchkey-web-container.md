- `pyproject.toml` pins `imbue-mngr-latchkey` and `imbue-mngr-forward` from the same mngr commit as every other mngr package, for the auth sandbox library's use of the desktop's sign-in wrapper.

- `system/scripts/register_vm_exec.sh` reads the container's default gateway from `/proc/net/route` when `ip` is absent, which it is in the product image: until now the script skipped registration on every slice ("no 'ip' command"), so no imbue-cloud workspace ever listed `vm-exec` in its service map and the web chrome could not provision the VM's latchkey gateway.

- `system/scripts/run_owner_exec.sh` takes owner-exec's listen address from `OWNER_EXEC_LISTEN_HOST` when it is set (`127.0.0.1` otherwise, as before); the web latchkey auth sandbox's boot script sets `0.0.0.0` so Modal's encrypted tunnel reaches the daemon.

- `system/scripts/setup_system.sh` installs latchkey 3.21.0 (was 3.19.4), the version the VM gateway package installs and the floor `imbue-mngr-latchkey` enforces on every run, so the auth sandbox image (which installs through this script) can run the sign-in at all.

- `system/scripts/install_owner_exec.sh` installs owner-exec v0.2.3 (was v0.2.2), which exposes its response-signature headers to the web chrome origin so the chrome can verify a VM's vm-exec answers. The monorepo's VM-install pin moves in lockstep.
