Added `specs/minds-release-channels/spec.md`, the design for user-selectable release channels (`stable`, `beta`, `alpha`) in the minds desktop app, and implemented phases 1 and 2 of it. The spec has been reconciled against the code it produced: it carries a numbered findings section for what building it disproved, and a verification log separating what was observed from what was only reasoned about.

Added `scripts/release_channel/` (next to `scripts/lima_image/`, the existing home for release tooling), in two pieces: `manifest.py` is the single-channel primitive -- the gates, the manifest rewrite, and the upload, with no entry point of its own -- and `publish.py` is the command that composes it over every channel the file declares. Pointing a channel at an already-built ToDesktop build copies that build's own update manifest and rewrites its URLs to absolute, so artifacts are never re-hosted and digests are never recomputed. Nothing is published unless the move is forward or `--allow-rollback` is passed, and unless the version is a plain `X.Y.Z`. On a tier that configures an image store, the pre-baked Lima image must also exist for the build's `FALLBACK_BRANCH` on each arch the run names (`--arch`, `aarch64` by default), whose absence is otherwise silent: creates quietly go from ~45s to ~5min.

`apps/minds/docs/release.md` gains a Release channels section: every channel, stable included, moves by editing `apps/minds/release-channels.toml`. Clicking Release in ToDesktop is needed once more, to hand the installs already in the field a build that reads our manifests at all; after that it governs only ToDesktop's own download page.

The spec's findings record two documentation errors the work turned up, both fixed here rather than deferred. `apps/minds/electron/paths.js` described the bundled `root_name` as the "production / staging / beta" case, but no beta tier was ever built -- the comment now says what the axis is, which is that a tier decides which infrastructure the app talks to and which data directory it owns, never which build it is offered. And `ClientEnvConfig`'s docstring cited a `PublicClientEnvConfig` guard that nothing in the repo defines; what actually keeps a committed `client.toml` secret-free is `write_client_config` naming every key it emits, one at a time, so the docstring now says so.

`scripts/r2/setup_tier.py` gained a `--kind` flag so one provisioner serves both R2 buckets: `lima-images` (the default, unchanged) and `update-feed`, which hosts the release-channel manifests at `minds-update-feed-<env>`, served from `updates.<domain>` in production. The bucket holds no releases -- the binaries stay on ToDesktop's CDN -- so it is named for the feed it serves, which matters because that hostname is compiled into every shipped binary and can never change. Each kind gets its own bucket and hostname per environment, so a publish of one can never overwrite the other and the bucket-scoped token minted for one cannot reach the other.

Promotion is now a pull request. `apps/minds/release-channels.toml` declares which build each fast channel serves, `scripts/release_channel/publish.py` makes that true, and `.github/workflows/minds-release-channels.yml` runs it -- nothing else writes a manifest. So a promotion is reviewable before it takes effect, the channel's history is the file's history, and undoing one is `git revert` rather than a remembered command. The declared version is checked against what the build actually is, so the diff a reviewer approves cannot say something different from what gets published.

The workflow is split by trust: the PR job runs every gate with **no credentials**, because each one reads a public URL, so review gets the real answer without a secret being reachable from PR-authored code; only the push-to-main job takes the R2 credential, behind the `minds-release` environment.

A tier that configures no Lima image store is a supported configuration, and the run now says so rather than skipping the image gate in silence -- a reviewer approving a promotion can see which gates actually ran.

`just test-minds-js` runs the minds JS suites -- the Electron shell's `node:test` units and the SPA frontend's vitest -- and a new `test-minds-js` job in CI runs it on PRs that touch either. Neither suite ran anywhere before, which mattered here: the only guard against re-enabling downgrades in the auto-updater is one of those unit tests.

Recorded the versioning and cut mechanics settled on 2026-08-13. An alpha cut is the same tagged release as a stable one -- bump, vendor-sync, prove green, tag both repos -- differing only in that the manifest moves instead of ToDesktop's Release action, and that the pre-baked Lima image and pool-host bake are skipped. Neither bake is a correctness gate: both degrade to a slow path rather than breaking, so the daily channel stays automatable and the operator-only work lands on the channels whose cadence can absorb it.

Two mechanics are load-bearing and now written down. `FALLBACK_BRANCH` names a tag that does not exist yet, because a pointer naming a dwt SHA has no fixed point -- `build_info.py` is inside the tree vendored into dwt, so committing the SHA changes the content the SHA is derived from. And the bump commit, not the tag, is what makes concurrent cuts safe: git rejects the second bump as non-fast-forward, so a collision surfaces in seconds rather than after a twenty-minute build. A failed cut therefore burns its version and never reuses it.

The provisioner moved from `scripts/lima_image/setup_tier.py` to
`scripts/r2/setup_tier.py`. It serves two kinds of bucket now, so a path saying
it belongs to the Lima image was pointing readers at the wrong thing -- as was
its docstring, which still described creating `minds-lima-images-<env>` and
nothing else.

The rollback gate now reads what a channel serves from the R2 bucket rather than
through the CDN, whenever the run holds a credential for it. The manifest is
published with a 60-second `max-age`, so a promotion run inside that window read
the *previous* one back through the public feed -- and the gate would compare
against a version the channel had already left, then wave through the backwards
move it exists to refuse. The bucket holds the object itself and is
read-after-write consistent, so it cannot be stale. The credential-less
`validate` job still reads the public feed, which is the one run that can afford
a stale answer because it publishes nothing; every run says which reader it used.
Installed apps are unaffected: they keep fetching the CDN copy, which is what the
TTL is for.

`scripts/r2/` now owns everything about R2 itself -- provisioning in
`setup_tier.py`, connecting in `client.py` -- while each feature directory owns
what it publishes. The credential read and the boto3 client had a copy in each
publisher and the copies had already drifted: the release-channel one named the
missing environment variable, the Lima image one raised a bare `KeyError` that
boto3 then reported as an endpoint failure. `scripts/lima_image/publish.py` now
names the missing credential too.

Reaching that shared client made `scripts/lima_image/publish.py` an intra-repo
importer, and a file run by path puts only its own directory on `sys.path` -- so
the command the runbook and the bake script give for it stopped resolving
`scripts.r2`, before parsing a single flag. Both now run it as
`uv run python -m scripts.lima_image.publish`, which is the form the rest of the
release tooling already uses, and a test refuses the path form anywhere it is
documented.

`just test-minds-js` now regenerates the frontend's types and typechecks them
before running the suites, and the CI job that calls it also triggers on the
Python the types are generated from. The frontend's TypeScript interfaces are
derived from the desktop client's pydantic models, so renaming a field there can
leave frontend code reading something that no longer exists -- and vitest
transpiles TypeScript without checking it, so the suites passed regardless.
Nothing else in CI ran `tsc`, which meant a frontend type error of any kind could
reach main. Verified by renaming a model field and watching the job fail on the
two frontend files that read it.

The publish job now names the events it runs on (`push`, `workflow_dispatch`)
rather than excluding pull requests. It is the job that holds the R2 credential
and puts a manifest in front of every user on a fast channel, so a trigger added
to the workflow later has to be named there before it can publish.
