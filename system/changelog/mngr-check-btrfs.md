- Fixed the pool/slice VM boot race where `minds-autostart.service` (ordered
  only `After=docker.service`) could run before the `/mngr-btrfs` data volume
  was mounted, so the workspace container failed its volume mount, the failure
  was swallowed by `|| true`, and nothing ever retried -- the workspace stayed
  down until an operator intervened (mngr-internal#266, hit on all 12 slices of
  box `9ef5ab2e` after its 2026-08-03 reboot). The service is now triggered by
  a `minds-autostart.path` unit gated on a readiness marker
  (`/mngr-btrfs/.minds-volume-ready`) that the installer creates exclusively on
  the mounted volume, behind a hard `mountpoint` check -- so the condition can
  never be satisfied by root-fs debris shadowed under the unmounted mountpoint
  (a stale snapshot-helper request replayed at boot once `mkdir`'d
  `/mngr-btrfs/snapshots` on the root fs pre-mount, which defeated an earlier
  `PathExistsGlob=/mngr-btrfs/*` condition in staging testing). The wake-up
  event is the symlink refresh lima's provision script performs right after
  mounting on every boot -- the same event-driven shape
  `scripts/minds_lima_autostart.sh` already uses for the lima direct-VM mode.
  Applied uniformly to the vultr, pool_host, aws, gcp, and azure templates
  (harmless on the loop-mount VPS modes, where the volume is up before any
  service starts and the unit simply fires at boot as before).

- Defense in depth against a premature trigger: the service sets
  `StartLimitIntervalSec=0` (previously, rapid re-triggers of a failing
  service tripped systemd's default start limit and the resulting
  `unit-start-limit-hit` permanently killed the path watcher -- observed on a
  staging box reboot), and `minds-outer-autostart.sh` now waits for
  `/mngr-btrfs` to actually be a mounted volume before touching docker
  (5s poll, 30 min hard bound, warning past 60s), so an early fire parks
  until the mount instead of burning start attempts.

- `minds-outer-autostart.sh` no longer swallows failures: a container that does
  not reach `running`, or a services-agent relaunch that fails, now fails the
  unit so the breakage is visible in `systemctl status minds-autostart`.
  `docker start` stderr now reaches the unit journal (previously discarded, so
  the real volume-mount error had to be dug out of dockerd's log).

- The installer is backfill-safe for the existing fleet: it removes the old
  direct `multi-user.target` enablement of the service, `reset-failed`s units
  a past boot may have left dead of the start limit, refuses to install when
  the volume is not mounted, and starts the path watcher immediately so
  re-running it on a live VM takes effect without a reboot.

- The gcp and azure boot units now use the same
  `/home/user/workspace/system/scripts/...` services-agent path as the other
  templates (their pre-restructure `/mngr/code/scripts/...` path had been
  silently broken by the `|| true`; upstream migrated those templates in the
  meantime and this branch adopts that path), so all five templates install
  byte-identical autostart machinery.

- Note: fleet VMs have the old unit baked in; this change only covers new
  bakes until the installer block is re-run on existing VMs (tracked as the
  rollout half of mngr-internal#266).
