Root-level support for the minds Mithril SPA migration: `scripts/snapshot_minds_e2e_state.py` now builds the SPA bundle (`pnpm install --frozen-lockfile && pnpm generate && pnpm build`) into the e2e snapshot image so Playwright launches exercise the real frontend.

The root ty config excludes `apps/minds/hatch_build.py` (it imports hatchling, which only exists inside hatchling's isolated build environment), and `.gitignore` covers the two new generated artifacts (`apps/minds/imbue/minds/desktop_client/static/ui/` and `apps/minds/frontend/src/generated/`).
