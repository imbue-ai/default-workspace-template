# How `system/vendor/mngr` is synced

`default-workspace-template` (DEFAULT_WORKSPACE_TEMPLATE) vendors a copy of the mngr
monorepo at `system/vendor/mngr/`. The DEFAULT_WORKSPACE_TEMPLATE Docker build installs the
container's `mngr` from that directory editable (`uv tool install -e
system/vendor/mngr/libs/mngr`, run by `system/scripts/build_workspace.sh`), so whatever
lands in `system/vendor/mngr/` *is* the mngr that runs inside every agent.

`system/vendor/mngr/` is a plain copied-in snapshot. It is **not** a git subtree and
**not** a git submodule -- never run `git subtree` or `git submodule` against it.

## Only the public subset may travel

`imbue-ai/default-workspace-template` is a **public** repo, and its contents are baked
into every workspace we hand a user. So the vendored tree is not the monorepo: it is
the monorepo's *public subset* -- byte for byte the same tree the Copybara mirror
publishes to `imbue-ai/mngr`.

That subset is defined once, in `mirror/copy.bara.sky` (`PUBLIC_FILES`, plus the
internal-block strip and the `mirror/overlay` move).
`scripts/public_subset.py` reads that definition -- it never restates it -- and
materializes the tree. Every sync path calls it, and nothing else may populate
`system/vendor/mngr/`.

Copybara itself is not usable on these paths: it needs a JVM and a committed ref,
while the dev loop has to filter an uncommitted working tree offline in seconds. The
two implementations are held together by the `Public subset matches copybara byte for
byte` step in `.github/workflows/mirror-gate.yml`, which runs the pinned Copybara jar
and diffs its tree against `scripts/public_subset.py`'s on every PR. If that step ever
fails, the vendored tree has stopped matching what the mirror publishes -- which is
how private files leak -- so fix the filter, never the diff.

## The two modes

| Mode | Form | Carries | Commits in DEFAULT_WORKSPACE_TEMPLATE? | Used for |
|---|---|---|---|---|
| **at a ref** | `public_subset.py DEST --ref <sha> --replace` | the public subset of committed content at an exact SHA; permissions preserved; reproducible | yes | releases |
| **working tree** | `public_subset.py STAGING` then `rsync -a --delete --checksum` | the public subset of the working tree, uncommitted edits included | no | dev iteration and pool bakes |

Use **at a ref** for a reproducible, committed snapshot tied to an exact mngr SHA (the
release flow). Use **working tree** to get your *uncommitted* local mngr changes into a
container without a commit (the dev loop, and baking a pool host from a working tree).

The working-tree mode materializes into a staging dir and then rsyncs with
`--checksum`. That matters: a freshly materialized tree has fresh mtimes on every file,
so a default (size+mtime) rsync would treat all 3,300 files as changed and re-transfer
them on every iteration. `--checksum` keeps unchanged files' mtimes at the destination,
which also preserves Docker layer-cache hits on re-bakes.

## Every path that populates it

| Path | Where | Mode | Trigger |
|---|---|---|---|
| `just sync-vendor-mngr` | `private.just` | at a ref (`HEAD`) | releases |
| `sync_vendor` job | `.github/workflows/minds-launch-to-msg.yml` | at a ref | launch-to-msg; pushes to the public DEFAULT_WORKSPACE_TEMPLATE `main` |
| `just sync-vendor-mngr-live` | `private.just` | working tree | every dev-app startup (`just minds-start` calls it), or on demand |
| `sync_mngr_into_template` | `apps/minds_admin/.../bake/pool_bake.py` | working tree | `minds-admin pool create --mngr-source ...` |
| `propagate_changes` | `apps/minds/scripts/propagate_changes` | working tree | each dev-loop iteration into a running container |
| `_vendor_mngr_into_default_workspace_template` | `apps/minds/imbue/minds/desktop_client/default_workspace_template_worktree.py` | at a ref (`HEAD`) | workspace-creation tests (snapshot bake, create+chat, full flow) |

Adding a new one means calling `scripts/public_subset.py`; do not hand-roll a filter.

Both at-a-ref callers that commit also run `uv lock` in the DEFAULT_WORKSPACE_TEMPLATE root
and commit the result with the snapshot. DEFAULT_WORKSPACE_TEMPLATE's root `uv.lock` pins the
vendored mngr libraries as editable path deps (`imbue-mngr`, `imbue-common`,
`resource-guards`, `concurrency-group`, `mngr_claude`, ...) and records their resolved
`requires-dist`, so a snapshot that moves any of their dependencies strands it. That
lock is the DEFAULT_WORKSPACE_TEMPLATE root's own, not the `system/vendor/mngr/uv.lock`
inside the snapshot, which the sync leaves untouched -- so the vendor-match invariant
(`system/vendor/mngr` equals the public subset of its mngr SHA, blob for blob) still
holds. The full release procedure, including that invariant, is in
`apps/minds/docs/deploy/release.md`.

`propagate_changes` additionally protects `data/`, `.mngr/`, and
`.claude/settings.local.json` from deletion when rsyncing into `/home/user/workspace/`.

The desktop client's Create flow performs a *separate* rsync -- the DEFAULT_WORKSPACE_TEMPLATE
worktree over a shallow clone into `/home/user/workspace/` -- not a
monorepo->`system/vendor/mngr` sync.

### The synced copy is meant to be uncommitted

`just sync-vendor-mngr-live` deliberately leaves the DEFAULT_WORKSPACE_TEMPLATE worktree dirty; the
code-guardian stop hook exempts `system/vendor/mngr` from its commit check
(`stop_hook.uncommitted_exempt_paths`) for exactly this reason. Git does not
honor that exemption, though: it refuses to merge over working-tree state the
merge would overwrite, which is what happens whenever a release-time vendor
refresh is in the incoming range. The hook reports that case on its own (`Merge blocked by uncommitted
changes under an exempt path`) rather than as a merge conflict. Drop the copy,
merge, and re-run `just sync-vendor-mngr-live` -- never commit it from the dev
loop, and never resolve it as a conflict. See the mngr `CLAUDE.md` section
"Vendored mngr in the default-workspace-template worktree" for the commands.

## `system/vendor/tk`

`system/vendor/tk/` is a forked-and-modified copy of the
[tk](https://github.com/wedow/ticket) ticket tracker. We maintain it by hand and
upgrade it manually; we do not pull from upstream. Like `system/vendor/mngr`, it is a
plain snapshot -- not a subtree or submodule.
