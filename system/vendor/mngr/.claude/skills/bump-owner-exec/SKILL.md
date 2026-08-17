---
name: bump-owner-exec
argument-hint: "[version]"
description: Bump the pinned owner-exec daemon version. The version is pinned in two repos (the monorepo VM-install and the default-workspace-template in-container install), and the shared crypto vectors are vendored in two more places; this skill covers all of them. Use when asked to "bump owner-exec", "update owner-exec to X.Y.Z", or after a new owner-exec release lands.
---

# Bump the owner-exec version pin

The owner-exec daemon is built and released from the `imbue-ai/owner-exec`
repo. Consumers fetch the pinned release binary and verify its published
sha256. The version is pinned in **two** places across two repos, and the
shared test vectors are vendored in **two** more.

## Pick the version

Use the version the user named. Otherwise use the latest release:
`gh release view --repo imbue-ai/owner-exec --json tagName --jq .tagName`.

## Bump the two version pins

Both are plain constants; edit them to the new tag (e.g. `v0.2.0`).

1. **Monorepo VM install** -- `OWNER_EXEC_VERSION` in
   `libs/mngr_latchkey/imbue/mngr_latchkey/owner_exec_vm.py`. Its unit tests
   (`owner_exec_vm_test.py`) assert the install command references it.

2. **default-workspace-template in-container install** -- `OWNER_EXEC_VERSION`
   in `system/scripts/install_owner_exec.sh` (in the dwt checkout, usually
   `.external_worktrees/default-workspace-template`). This is in another repo
   and is checked by nobody; keep it in lockstep by hand.

Keep the two in lockstep: the inner (container) and vm (outer) instances should
run the same daemon build.

## Refresh the vendored vectors (only if the profile/vectors changed)

If the release changed the wire profile or regenerated `vectors/vectors.json`,
re-vendor the shared vectors from the owner-exec repo into both consumers:

- `libs/imbue_common/imbue/imbue_common/owner_exec_vectors/vectors.json`
- `apps/remote_service_connector/frontend_web/src/crypto/owner_exec_vectors.json`

Copy the exact file from the owner-exec release/commit so the Python
(`owner_exec_client_test.py`) and TypeScript (`crypto/ed25519.test.ts`) vector
tests validate against the shipped profile. A profile change with no vector
refresh will fail those tests.

## Verify

- `cd libs/mngr_latchkey && uv run pytest imbue/mngr_latchkey/owner_exec_vm_test.py`
- `cd libs/imbue_common && uv run pytest imbue/imbue_common/owner_exec_client_test.py`
- `cd apps/remote_service_connector/frontend_web && npx vitest run src/crypto/ed25519.test.ts`

Add a changelog entry per touched project.
