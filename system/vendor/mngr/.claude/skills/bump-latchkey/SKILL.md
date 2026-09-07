---
name: bump-latchkey
argument-hint: "[version]"
description: Bump the pinned upstream latchkey CLI version. The version is pinned in four separate places (bundled npm dep, minimum locally-installed version, remote-VPS install, in-workspace install); this skill covers all of them. Use when asked to "bump latchkey", "update latchkey to X.Y.Z", or after a new latchkey release lands.
---

# Bump the latchkey version pin

## Pick the version

Use the version the user named. If they did not name one, use the latest
release: `npm view latchkey version`.

## Bump the pins

Four pins, edited independently. Bump 1, 3, and 4 unconditionally. Bump 2 only
after asking (see below).

They are not free of each other: **pin 2 must never exceed pin 1 or pin 3**. Above
pin 1, the minds app rejects the very latchkey it ships; above pin 3, the pins
have simply drifted. Never raise pin 2 alone -- if asked only to raise the floor,
raise the others to at least match. Tests in
`libs/mngr_latchkey/imbue/mngr_latchkey/remote_gateway_test.py` and
`apps/minds/imbue/minds/test_latchkey_version_alignment.py` enforce that, so a
half-finished bump fails CI. Pin 4 is in another repo and is checked by nobody.

1. **Bundled with the minds app** -- `latchkey` under `dependencies` in
   `apps/minds/package.json`, then refresh the lockfile with
   `bash -c '. apps/minds/scripts/select_node_version.sh && cd apps/minds && pnpm install --lockfile-only'`.
   Check that `apps/minds/pnpm-lock.yaml` picked up the new version and nothing else.

2. **Minimum locally-installed version** -- `LATCHKEY_MIN_VERSION` in
   `libs/mngr_latchkey/imbue/mngr_latchkey/core.py`. This is the floor enforced
   against a user's own CLI when they use minds infrastructure without the minds
   app, so raising it forces those users to upgrade. **Ask the user whether to
   bump this one**, telling them what the new release contains and whether
   anything in the repo depends on it. If they say yes, update the comment above
   the constant too -- and if there is no code dependency on the new release,
   say so there rather than implying one.

3. **Installed on remote VPS hosts** -- `LATCHKEY_VERSION` in
   `libs/mngr_latchkey/imbue/mngr_latchkey/remote_gateway.py`. Existing hosts
   upgrade automatically on the next minds start.

4. **Installed inside workspaces** -- `LATCHKEY_VERSION` in
   `system/scripts/setup_system.sh` in the `default-workspace-template` repo.
   That is a separate repo: work in it via
   `just default-workspace-template-worktree`, which creates
   `.external_worktrees/default-workspace-template` on the current branch.
   Commit and push there too, then tell the user the branch needs its own PR
   and merge -- that worktree is gitignored here, so a commit left in it reaches
   nobody. `apps/minds/docs/deploy/ops/app-release.md` covers how the template then reaches
   users.

## Finish

Add a changelog entry for every project touched: `apps/minds`,
`libs/mngr_latchkey`, `dev` (if you edited this skill), and `system` in the
default-workspace-template worktree.
