- `AGENTS.md`'s "Dealing with the unexpected" section points at the `check-app-errors` and `assist` skills, and its test-gate pointer follows the harden worker to `.agents/skills/harden-worker/`.

- The worker create template's comment in `.mngr/settings.toml` and `system/test_mngr_template_stacking.py` describe the harden worker as a tracked skill the checkout carries, still never an installed copy.

- `docs/system/workspace-internals.md` follows the new path, and the stale `docs/system/specs/skill-lifecycle-shared-refs/` spec is removed.
