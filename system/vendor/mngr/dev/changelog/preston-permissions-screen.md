`just minds-start` now builds the desktop client's single-page app before launching Electron.

The app server serves the SPA from `static/ui/`, which is produced at wheel-build time and is gitignored — but `pnpm start` only builds the legacy JinjaX stylesheet. A dev run therefore served whatever bundle happened to be built last, or 404'd on a fresh worktree, with no indication that the UI on screen was stale.
