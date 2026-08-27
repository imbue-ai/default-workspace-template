Remote (imbue_cloud) workspaces now run their container under gVisor (`runsc`) from the bake, with `/run` and `/tmp` on tmpfs (mngr-internal `new-fleet-runsc-prototype`, slice-fleet cutover phase 1).

- `CLAUDE.md` / `AGENTS.md`: a "Sandboxed runtime" section telling the agent what does not work inside the sandbox (ptrace tooling, eBPF, FUSE, io_uring, nested container runtimes, unusual ioctls) and that filesystem-metadata-heavy operations are several times slower.

- `.mngr/settings.toml`: the `[providers.docker]` comment no longer claims the ovh/vultr/imbue_cloud host setup applies the tmpfs mounts (it never did for slices; the slice bake's `-S` overrides do).

- `.mngr/settings.toml`: `auto_dismiss_dialogs` -> `auto_dismiss_dialogs_at_startup` for the claude, codex, pi-coding, and antigravity agent types, following the mngr harness plugins' rename (the old name fails the settings load with current mngr).

- host_backup keeps one on-host btrfs snapshot by default (`max_local_snapshots` 5 -> 1): restic reads only the newest, and each retained snapshot pins its copy-on-write delta on the slice's data disk, which now also holds docker's data-root under a single workspace quota.
