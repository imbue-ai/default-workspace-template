AGENTS.md now says that a slow backup comes from how many files the home tree holds, not their size, and points to the host_backup README's "Slow backups" section and `uv run host-backup-heavy-dirs`.

`data/system/README.md` no longer calls `backup.toml`'s `excludes` "extra": they replace the defaults, and the new `extra_excludes` adds to them. `data/.apps/README.md` notes what of an app's data the backup leaves out: a directory holding `CACHEDIR.TAG`, and any directory matching an exclude pattern (such as `build`, `dist` or `.cache`), which therefore must never hold user data.
