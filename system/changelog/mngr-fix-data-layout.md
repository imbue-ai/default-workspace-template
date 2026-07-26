The workspace moves to the /home/user user-data layout on Debian trixie with a fully pinned, convergeable environment:

- One persistent tree: the per-host volume backs `/home/user` -- the repo checkout at `~/workspace`, worktrees at `~/worktrees`, mngr state hidden at `~/.mngr`, dotfiles and overlay data beside them. root's home moves there (`usermod` in the image; provisioning on lima/modal). No `/mngr`, `/code`, or `/worktree` paths remain (hard cutover, no compat symlinks); `~/.mngr/layout-version` stamps every backup.

- Caches stay off backups by construction: `XDG_CACHE_HOME`/`npm_config_cache` point at `/var/cache/user` and the seed script symlinks `~/.cache` there; `/tmp` is tmpfs.

- Debian trixie base with snapshot-pinned apt: every apt operation resolves against the archive frozen at the committed `.mngr/apt-snapshot-timestamp` (imbue's mirror when `APT_MIRROR_BASE_URL` is set, snapshot.debian.org otherwise). No third-party apt repos remain: nodejs comes from trixie main, gh installs as a pinned sha256-verified binary.

- New `libs/env_converge`: the environment record (apt via a Post-Invoke hook, npm -g and uv tool probes, per-source JSON under `~/.mngr/plugin/env-converge/`) plus two-phase boot convergence -- bootstrap applies overlay symlinks pre-services, and the `env-converge` one-shot (replacing `deferred-install`, no more marker files) runs `scripts/env.d/` units and re-installs anything the record has that the rootfs lacks. `env-converge upgrade` is the one version-advancing operation, bundled into update-self.

- host_backup covers the whole home tree and excludes the provisioning-owned `authorized_keys`; mngr's plain-text service logs move to `/var/log/mngr`.
