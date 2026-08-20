---
name: minds-justfile
description: Use the root justfile as the canonical entry point for ANY minds task -- minds app (desktop client), pool hosts, minds environments (activate/deploy/destroy), minds deployments, and minds tests. Before running ad-hoc `uv run minds ...` / `uv run minds-admin ...` / `mngr imbue_cloud ...` commands, check the justfile for a named recipe; if none exists for the task, ADD one. Use whenever the request involves the minds app, pool/leased hosts, a minds env/tier (dev/staging/production), or a minds deploy.
---

# Minds tasks go through the justfile

The root `justfile` is the canonical, auditable, named home for every
operational minds task. Recipes encode the right flags, the right env-var /
Vault wiring, and the activation guards -- so they "just work" and stay
reviewable. Hand-rolled `uv run minds ...` / `uv run minds-admin ...` / `uv run mngr imbue_cloud ...`
invocations drift, leak secrets, and miss steps (e.g. deriving the pool
management key from Vault, passing the host_pool DSN for staging/production).
This is the same class of mistake as hand-exporting the pool DSN and
SSH key instead of letting the env-aware `minds-admin pool create`
resolve them from the activated tier's Vault entries.

## The rule

When a task involves any of: the **minds app / desktop client**, **pool hosts /
leased mode**, a **minds environment or tier** (dev / staging / production),
a **minds deployment**, or **minds tests** --

1. **Look in the justfile first.** Run `just --list`, and/or
   `grep -nE 'minds|pool|deploy|env' justfile`. Read the recipe's leading
   comment block -- it documents prerequisites (almost all require an
   activated env) and usage.
2. **Use the recipe.** Prefer `just <recipe> ...` over the underlying command.
3. **If no recipe fits, ADD one.** Write a new, well-commented recipe that
   wraps the canonical command, then use it. Keep the recipe thin -- push any
   credential/secret resolution into the env-aware Python CLI rather than
   reimplementing it in bash. Do not paper over a missing recipe with a one-off
   shell command -- the point is a named, auditable script that the next person
   (or agent) can audit and re-run. Fix stale recipes you encounter the same way.
4. **Keep secrets out of argv where the wrappers already handle it.** The
   minds env-aware CLIs read OVH creds, the pool management key, and the
   staging/production host_pool DSN from Vault themselves (Vault addressing via
   `apps/minds/imbue/minds/envs/vault_reader.py`, which defaults
   `VAULT_ADDR`/`VAULT_NAMESPACE` to the HCP cluster). Don't re-export those by
   hand.

## Almost everything requires an activated minds env

Most minds recipes refuse to run without an activated env, by design:

```bash
eval "$(uv run minds-admin env activate <name>)"      # use-only (mngr/minds run, pool, tests)
eval "$(uv run minds-admin env activate --deploy <name>)"   # deploy mode (env deploy/destroy/recover)
```

`<name>` is `dev-<your-user>` for a personal dev env, or `staging` /
`production`. Deploy-mode (`--deploy`) additionally pins `MODAL_PROFILE`; it's
required only for `minds-admin env deploy/destroy/recover`.

## Current minds-relevant recipes (run `just --list` for the live set)

Environments / deploy:
- `just deploy [args]` -- `minds-admin env deploy` for the activated env (tier
  deploys need `--yes-i-mean-<tier>`).

Pool hosts (leased mode):
Pool hosts are baked as bare-metal **slices** (lima/QEMU VMs carved on a
pre-registered + prepped bare-metal box). Baking new OVH classic VPS pool hosts
is DEPRECATED and no longer supported; existing OVH VPS rows stay listable and
destroyable. First order + set up a box with `just order-server` (pass
`--dry-run` first for the no-charge price preview), `just await-delivery
<server-id>`, and `just setup-server <server-id>` (or `minds-admin server
register` for an already-provisioned box); the box must be `ready` with a
free slot. These are env-aware: OVH creds, pool DSN, and pool SSH key resolve
from the activated tier.
- `just bake-slice-dev <region> [workspace_dir] [count] [extra flags]` -- DEV
  bake from a working tree; the stamped identity (`repo_url` + `repo_branch_or_tag`)
  is DERIVED from the folder's `origin` remote + current branch (best-effort label,
  uncommitted changes included). Pass `--server-id <id>` for the box to bake onto.
- `just bake-slice-prod <region> <tag> [count] [extra flags]` -- PRODUCTION
  bake: clones the DEFAULT_WORKSPACE_TEMPLATE remote at an exact `<tag>` and bakes from that (content
  provably equals the tag); identity = canonical remote + tag. Pass `--server-id`.
  - Identity is never hand-typed in `--attributes` (those are non-identity only,
    e.g. resources). For a DEV fast-path match, the create form's repository must be
    the ACTUAL git remote + the baked branch -- a local clone path resolves to the
    same canonical remote, but the form value the client sends must match. Extra
    flags forward to `minds-admin pool create` (e.g. `--mngr-source`).
- `just list-pool-hosts` -- list `pool_hosts` rows for the activated env.
- `just list-servers` -- list bare-metal servers with slot accounting for the
  activated env (no manual DSN export needed). The slot columns come from THIS
  env's `pool_hosts` rows only, so a box shared with another env reads as emptier
  than it is -- use `just audit-boxes` before concluding you have free slots.
- `just audit-boxes` -- SSH every bare-metal box for the activated env and report
  its real occupancy (all envs' slices) plus any cross-tier contamination: a slice
  stamped for another tier, or an extra key in the lima user's `authorized_keys`.
  A bake onto such a box refuses; this finds one without a failed bake. Read-only.
- `just prep-server <server-id>` -- (re-)prep a bare-metal box for slice baking;
  pool SSH key + DSN resolved from the activated tier automatically. Installs +
  verifies the observability collector when the tier has a boxes ingest
  credential in Vault (fail-closed; clean skip otherwise). Idempotent;
  also how pre-2026-06-27 boxes get the DEFAULT_WORKSPACE_TEMPLATE image cache dir that production
  `--from-tag` bakes require.
- `just setup-server <server-id>` -- provision a delivered box to `ready`:
  destructive OS reinstall (injects our host key), then the same composed prep
  as `prep-server`. `just order-server [flags]` / `just await-delivery
  <server-id>` cover the ordering steps before it.
- `just destroy-pool-hosts <pool-host-id> [<pool-host-id> ...]` -- tear down the
  named hosts in parallel: atomically claim each row (a user cannot lease it
  mid-destroy), destroy its slice lima VM, and drop the row (manual teardown, e.g.
  retiring old rows after baking a new pool generation; steady-state release is
  automatic via the connector, and `minds-admin env destroy` tears down a whole tier).

Desktop client / dev loop:
- `just minds-start` / `just minds-stop` / `just minds-build`
- `just propagate-changes <agent>` -- sync local mngr into a running Docker agent.
- `just forward-system-interface <agent>` -- Cloudflare tunnel for an agent.
- `just sync-vendor-mngr-live [default_workspace_template]` -- rsync the live mngr working tree into `system/vendor/mngr` in default-workspace-template (uncommitted, dev loop). `just minds-start` runs this at launch; run it directly to re-sync without relaunching.
- `just sync-vendor-mngr [default_workspace_template]` -- sync `system/vendor/mngr` in default-workspace-template via `git archive` (committed snapshot; release flow). See `apps/minds/docs/vendor-mngr-sync.md`.
- `just create-new-mind-repo <name> [parent_dir]` -- new private DEFAULT_WORKSPACE_TEMPLATE clone.

Tests:
- `just minds-test-deployment [args]`, `...-cleanup`, `...-up`, `...-down`,
  `minds-test-services-against`, `minds-test-deployment-only`,
  `just minds-test-electron`, `just test-offload-minds-snapshot <image-id>`.

## Related skills

- `minds-dev-workflow` -- the end-to-end dev iteration loop (uses these recipes).
- `release-minds` -- cut a minds release.
