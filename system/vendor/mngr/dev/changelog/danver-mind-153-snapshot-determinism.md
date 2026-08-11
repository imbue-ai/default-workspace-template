The minds snapshot producer (`scripts/snapshot_minds_e2e_state.py`) no longer
runs the workspace's deferred browser install at all: it sets
`DWT_SKIP_BROWSER_UNIT=1` via `MINDS_EXTRA_PASS_HOST_ENV`, which rides `mngr
create --pass-host-env` into the host env file on the workspace's persistent
volume -- so the env.d browser unit skips itself on every boot, including in
resumed test sandboxes. Snapshot builds stop paying the hundreds-of-MB
Fortress download and are deterministic by construction, and the bounded
deferred-install wait added by PR #215 is removed (Xvfb is now baked into the
default-workspace-template image, so no supervisord service depends on the
skipped unit). Closes the plan in mngr-internal#218 / MIND-153; pairs with
the default-workspace-template branch of the same name.

`apps/minds/resources/` (the per-platform restic/git bundles built by
`ensure-binaries.js`) is now also ignored in the root `.gitignore`:
`_generate-dockerignore` derives `.dockerignore` from the root file only, so a
macOS-built bundle in a local checkout was rsynced into the Linux offload
image, shadowed the sandbox restic with Exec-format errors, and -- via
offload's shared image cache -- could poison CI runs keyed to the same
checkpoint commit.
