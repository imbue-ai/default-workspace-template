Integration branch combining the workspace-layout trains (`mngr/fix-data-layout`, `mngr/declutter-template`) with `mngr/fix-apt-mirror`; the full per-train details live in this project's sibling entries for those branches.

For this project: the github-sync service becomes a wiring + visibility watchdog for the post-commit auto-push hook (`data/` is backup-covered, not GitHub-synced), with its config at `data/system/github_sync.toml`.
