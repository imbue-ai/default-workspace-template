# minds-v0.3.16 (2026-08-17): verified on dev, folded into 0.3.17

Never deployed to staging or production as itself; its content shipped inside
minds-v0.3.17 the same day. Verified end to end on the `dev-josh-1` env
(deploy_id `20260817T135749Z`), exercising the REAL upgrade path: a fresh
env deployed at v0.3.11 (migrations 000-017), then upgraded (018-026).
Final tags: mngr `8530898951`, default-workspace-template `2d7e7d90` (the
dwt tag was force-re-cut twice; see below).

## The uv.lock postmortem (the important lesson)

The first cut shipped a **stale root dwt `uv.lock`** (missing
`python-frontmatter`, `imbue-mngr-codex`, `imbue-mngr-pi-coding`). Every
build path runs `uv sync --all-packages --frozen`, so workspaces built from
the tag had a `mngr` CLI that crashed at import. Chain of custody: a lock
regen was lost in a merge; dwt CI ran plain `uv sync` (which silently
UPDATES a stale lock); a session agent then deliberately reverted the
accidental re-fix citing "no lockfile churn". Fixes: the lock was
regenerated with the pinned uv, and dwt CI gained a **`uv lock --check`**
gate. Lesson: before reverting lock "churn", run `uv lock --check` -- if the
committed lock is stale, the churn IS the fix.

## Cross-version update-self fixes (dwt PR #436; verified live)

The cross-version flow has an OLD lead follow the NEW staged SKILL.md while
launching with its OLD `create_worker.py`. v0.3.16 had removed `lead_agent`
from the update-self task template, so the worker could not deliver its
report and the lead silently waited out its full 90-minute await. Fixes now
in dwt main: `lead_agent` restored in the template (with a drift test),
tolerant frontmatter parsing, same-repo fallback report delivery, awaits
fail fast (rc 76) on idle workers, and `run_owner_exec.sh` self-heals the
owner-exec install before exec.

A full v0.3.11 -> v0.3.16 lima workspace self-update passed end to end
(~81 min; with a clean tag ~35 min). Identified-but-unimplemented speed
levers: a lockfile-only carve-out for the full review gates, skipping
upstream suites on byte-identical trees, in-workspace reviewer gates, and a
deterministic manifest-fold helper.

## Other durable changes

- Dev-tier deploys must set `MINDS_WEB_TEMPLATE_REF` explicitly (the
  committed dev `deploy.toml` no longer pins a template ref); shared tiers
  resolve `FALLBACK_BRANCH`.
- Post-update, four features (codex/pi harnesses, self-hosted sharing stack,
  owner-exec substrate, browser media pipeline) fully activate only after a
  workspace recreate.

## Gotchas worth keeping

- Pushing `.github/workflows/` changes to dwt fails over https (OAuth token
  lacks `workflow` scope) -- push over SSH.
- `uv run` in a fresh monorepo worktree without `uv sync --all-packages`
  first silently resolves `minds`/`modal` from another checkout's venv.
- Never launch the desktop app as a harness-tracked background task (a
  stopped task SIGTERMs its process group); launch detached via
  `setsid nohup just minds-start-cloud ... &`.
