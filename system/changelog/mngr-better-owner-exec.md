- Replaced the in-container Python `owner-exec` service with the pinned Go
  binary from `imbue-ai/owner-exec` (v0.2.0), installed at image build via
  `system/scripts/install_owner_exec.sh` and run by supervisord through
  `system/scripts/run_owner_exec.sh` in its "inner" role. Requests are now
  signed per the RFC 9421/9530 strict profile (previously a homegrown v1
  envelope), and every response is signed with the container's SSH host key.
  The service accepts both its host-id-scoped `container:<host-id>` audience and
  the workspace share domain, so exec works whether or not the workspace is
  shared. The endpoint surface (`/run`, `/read-file`, `/write-file`, `/grants`
  with CAS, `/_alive`) is unchanged.

- Added a one-shot `vm-exec-register` supervisord program
  (`system/scripts/register_vm_exec.sh`) that registers the VM-resident
  owner-exec service (`vm-exec`) in `apps.toml` when the workspace runs on an
  imbue-cloud slice / VPS outer, so the hosted web client can drive VM-level
  configuration (latchkey and other components that run outside the container).
  A no-op on local docker/lima workspaces.
