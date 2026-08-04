- Fixed the pool/slice VM boot race where `minds-autostart.service` (ordered
  only `After=docker.service`) could run before the `/mngr-btrfs` data volume
  was mounted, so the workspace container failed its volume mount, the failure
  was swallowed by `|| true`, and nothing ever retried -- the workspace stayed
  down until an operator intervened (mngr-internal#266, hit on all 12 slices of
  box `9ef5ab2e` after its 2026-08-03 reboot). The service is now triggered by
  a `minds-autostart.path` unit gated on `PathExistsGlob=/mngr-btrfs/*` (the
  per-host entries only become visible once the data disk is mounted; the
  symlink refresh lima's provision script performs right after mounting on
  every boot provides the wake-up event), the same event-driven shape
  `scripts/minds_lima_autostart.sh` already uses for the lima direct-VM mode.
  Applied uniformly to the vultr, pool_host, aws, gcp, and azure templates
  (harmless on the loop-mount VPS modes, where the volume is up before any
  service starts and the unit simply fires at boot as before).

- `minds-outer-autostart.sh` no longer swallows failures: a container that does
  not reach `running`, or a services-agent relaunch that fails, now fails the
  unit so the breakage is visible in `systemctl status minds-autostart` instead
  of reporting success with the workspace down. The installer also removes the
  old direct `multi-user.target` enablement of the service (backfill-safe) and
  starts the path watcher immediately so re-running it on a live VM takes
  effect without a reboot.

- Fixed the stale services-agent path in the gcp and azure templates' boot
  units (`/mngr/code/scripts/...` -> `/mngr/code/system/scripts/...`): the
  July tree restructure missed these two blocks, and the `|| true` swallowed
  the resulting exec failure, so the relaunch had been silently broken there.

- Note: fleet VMs have the old unit baked in; this change only covers new
  bakes until the installer block is re-run on existing VMs (tracked as the
  rollout half of mngr-internal#266).
