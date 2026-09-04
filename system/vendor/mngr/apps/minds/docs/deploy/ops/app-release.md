# Releasing a new minds.app

A release ships three pinned artifacts that must agree:

| Artifact | Pinned where |
|---|---|
| mngr code | a `main` SHA, tagged `minds-v<version>` |
| default workspace template | the `minds-v<version>` tag on `default-workspace-template` `main` |
| `.app` bundle | a ToDesktop build keyed by that mngr SHA |

Both repos tag with the **`minds-v<version>`** prefix (e.g. `minds-v0.3.1`), namespacing minds releases from each repo's own `v<version>`. The shipped binary clones the DEFAULT_WORKSPACE_TEMPLATE tag at runtime via `FALLBACK_BRANCH` in `apps/minds/imbue/minds/build_info.py`; tag immutability pins a binary to the snapshot it was verified against.
Both repos release from **`main`**. Neither `main` is branch-protected, so a PR is **never a merge gate** — you can push or merge to `main` directly. Its only role here is as a **CI surface**: `ci.yml` runs on PRs (any branch) and on push to `main`, *never on a bare branch push*, so opening a PR is how you get traditional CI on a release branch. Nothing is opened for human *review* unless real code rides along. Each repo gets a short-lived **release branch** (mngr: the version bump; DEFAULT_WORKSPACE_TEMPLATE: the `system/vendor/mngr` refresh); you prove the pair green, land both on `main`, tag each `main`, then re-prove green against the tags. **Green CI on the tags concludes the artifacts**; the staging rehearsal concludes the release and moving a channel is what ships it -- both sequenced by the `release-minds` skill.

## How this document is organized

Two halves. **Procedure** — Session setup through step 9, numbered and executed
in order; the numbers are stable because other sections cite them. **Reference**
— everything else, not optional but read when a decision comes up inside a step.

Mechanism other docs own is cited, not restated: [pool-hosts.md](./pool-hosts.md),
[services.md](./services.md), [../reference/environments.md](../reference/environments.md),
[../setup/vault.md](../setup/vault.md).

## Which step is this release already at?

The `release-minds` skill sequences the whole release. Within *this* doc:

```bash
grep -m1 '"version"' apps/minds/package.json                    # version being cut
git tag -l 'minds-v*' | sort -V | tail -3                       # cut on mngr?
git -C "${DEFAULT_WORKSPACE_TEMPLATE:?see Session setup}" tag -l 'minds-v*' | sort -V | tail -3
gh run list -R imbue-ai/mngr-internal --workflow=minds-launch-to-msg.yml -L 5 \
  --json databaseId,conclusion,createdAt,displayTitle
```

Version not bumped or a tag missing → step 1. Both tags exist without a green
launch-to-msg → step 8. Green → done here until promotion (step 9).

A run reporting success in **under ~2 minutes** was a marker **cache skip**, not
a verification — check its job durations.

**Treat a tag as immutable once anything has run against it.** Never move
`minds-v<version>` — take the next number. Downstream caches key on the tag
*name*, so a moved tag makes a later pool bake report success while baking the
previous code ([pool-hosts.md](./pool-hosts.md)). Version numbers are free.

## The two release branches

| Repo | Carries | Open a PR? |
|---|---|---|
| `mngr` | version bump (`apps/minds/package.json`), `FALLBACK_BRANCH` (`imbue/minds/build_info.py`), any mngr/minds code | Optional. Traditional CI on an inert bump is redundant with a green `main`, so a PR adds little — open one for a record, or when the branch also carries mngr/minds code you want CI/review on. |
| `default-workspace-template` | `system/vendor/mngr/` archived from the green mngr SHA, plus any consumer (`system_interface`) changes that vendor requires | Yes — as a **CI surface, not a review**. A pure vendor refresh isn't read, but a PR is the only way to run `ci.yml`'s `test` job (`uv sync` + `system_interface` tests) on the branch, which catches a `uv`-resolution or `system_interface` break fast on a big vendor jump. (You *can* skip it and lean on launch-to-msg, which covers the same end-to-end, just slower.) |

**Vendor-match invariant.** DEFAULT_WORKSPACE_TEMPLATE `system/vendor/mngr` must be the `git archive` of the *exact* mngr SHA it's paired with — the `commit_sha` you verify and the mngr SHA you tag. The binary runs the mngr SHA; the in-VM agent imports `system/vendor/mngr`. If they diverge, the agent's mngr can mismatch the binary's API (how the `system_interface` → `send_message_to_agents` break slipped in). Re-archive whenever the mngr SHA changes. When iterating on CI this means dispatching a `template_ref` whose `system/vendor/mngr` is synced to the SHA you're building — never DEFAULT_WORKSPACE_TEMPLATE `main`, which lags: a stale vendor silently rejects a field the binary renamed, so the in-VM agent never starts and the e2e wedges at "Waiting for initial chat agent…" (looks like a frontend hang, is really vendor skew; seen for `use_env_config_dir` → `isolate_local_config_dir`). `just sync-vendor-mngr` produces a matching DEFAULT_WORKSPACE_TEMPLATE branch.

> The Apple-Silicon lima-VZ `cryptography` SIGILL is handled in the default workspace template by `OPENSSL_armcap=0` (`.mngr/settings.toml` `host_env__extend` + `system/scripts/build_workspace.sh`), which skips OpenSSL's SVE CPU-cap probe. mngr does not pin `cryptography`.

**The DEFAULT_WORKSPACE_TEMPLATE vendor refresh is not reviewed.** The `system/vendor/mngr` snapshot (thousands of files) is generated and verified by *reproduction*, not by reading: the step-6 vendor-match check (a `git ls-tree` blob-hash comparison of the DEFAULT_WORKSPACE_TEMPLATE vendor tree against the tagged mngr SHA's tree) proves it equals that SHA file-for-file. A clean comparison *is* the review. (`system/vendor/mngr/**` is `linguist-generated` in DEFAULT_WORKSPACE_TEMPLATE's `.gitattributes`, so GitHub also collapses it.) The branch exists only to (a) stage the refresh so launch-to-msg can verify the (binary, template) pair **before** it lands on `main`, and (b) be the commit the tag points at. If a `system_interface` consumer fix rides along, isolate the vendor refresh in its own commit (`system/vendor/mngr: refresh from mngr <sha>`) so the real code is reviewable on its own — that fix is the only part anyone reads.

## File reference

| What | Where |
|---|---|
| Version string | `apps/minds/package.json` `version` |
| Baked DEFAULT_WORKSPACE_TEMPLATE tag | `apps/minds/imbue/minds/build_info.py` `FALLBACK_BRANCH` |
| `default-workspace-template` checkout | `$DEFAULT_WORKSPACE_TEMPLATE` — your local clone; `just sync-vendor-mngr` (step 3) reads its path from a gitignored `apps/minds/.env`. See Session setup. |
| `mngr` monorepo checkout | `$MNGR` — wherever you cloned it; you run `just` / `git` from here. See Session setup. |
| Build / e2e CI | `.github/workflows/minds-launch-to-msg.yml` (`workflow_dispatch`) |
| Traditional CI | `.github/workflows/ci.yml` (auto on push) |

## Session setup

**Read [next_deploy.md](../next_deploy.md) first.** It is the running checklist of
things that must land in, or be done by, the next release — code that has to be
in the build, deferred work coming due, known mixed-fleet states. It is written
to be consumed and reset by the release that ships it; the `release-minds`
skill closes that loop at the end.

[environments.md](../reference/environments.md) and [vault.md](../setup/vault.md) own
how tiers, their Vault entries and their Modal workspaces are laid out. Set these
once for the whole session — later steps assume them:

- **Disable the code-guardian stop hook**, first, before anything else. It
  merges `origin/main` and *pushes* on every stop — here and in the
  `default-workspace-template` checkout holding the release's frozen
  `system/vendor/mngr` — rewriting the tree a release keeps pinned.
  ```
  /imbue-code-guardian:reviewer-disable
  ```
  Verify rather than assume; `.reviewer/settings.local.json` is gitignored, so an
  agent worktree only gets a create-time snapshot:
  ```bash
  cat .reviewer/settings.local.json
  ```
  An unexpected `Merge remote-tracking branch 'origin/main'` at the tip of a
  release branch means it was live and has already pushed: re-derive
  `GREEN_MNGR_SHA`, re-run step 3, and do not tag until step 6 is clean.
- **`GH_TOKEN`** (derived, per session) — `export GH_TOKEN=$(gh auth token --user weishi-imbue)`. Pre-flight any push with `gh api user --jq .login` → must print `weishi-imbue` (the keychain "active" account drifts between parallel agents).
- **`MNGR`** and **`DEFAULT_WORKSPACE_TEMPLATE`** — absolute paths to your `mngr` and `default-workspace-template` clones, used by the shell commands in steps 4/6/7: `export MNGR=/your/mngr DEFAULT_WORKSPACE_TEMPLATE=/your/default-workspace-template`.
- **`DEFAULT_WORKSPACE_TEMPLATE_DIR`** — the *same* `default-workspace-template` path, but consumed by `just sync-vendor-mngr` (step 3), which reads it from a gitignored `apps/minds/.env` (minds-scoped, never committed — only that recipe loads it, so no shell-rc edit and it reaches non-interactive agent shells; see `apps/minds/.env.example`):
  ```bash
  echo "DEFAULT_WORKSPACE_TEMPLATE_DIR=$DEFAULT_WORKSPACE_TEMPLATE" >> apps/minds/.env
  ```
  An agent: if `apps/minds/.env` doesn't already define `DEFAULT_WORKSPACE_TEMPLATE_DIR`, ask the user for their checkout path — don't guess.

## What actually gates a release

Four things must hold; only two need *new* CI.

1. **The binary from the release SHA works end-to-end** — `minds-launch-to-msg.yml`
   (step 4). `main` never runs it, so it is the release's only unique
   verification and its wall-clock long pole. Start it early.
2. **The dwt PR's `test` job is green** (step 2). It refreshes
   `system/vendor/mngr` and may carry a `system_interface` fix, so a
   `uv`-resolution or stale-API break surfaces here. `ci.yml` runs only on a PR
   or on `main`, so the dwt branch needs a PR as a CI surface.
3. **`system/vendor/mngr` equals the tagged mngr SHA** — the step-6 blob-hash
   comparison, not CI.
4. **The release survives a staging rehearsal** — *Verifying a release in a
   running workspace*. launch-to-msg only ever builds a clean machine, so it
   covers neither an existing workspace nor its upgrade path. The 0.4.3
   rehearsal caught a release-blocking sharing bug with the three CI gates green.

Green CI concludes the binary; the rehearsal concludes the release.

**Not new signal:** traditional CI on a version-bump-only mngr branch. Nothing
asserts the version literal or that `FALLBACK_BRANCH` resolves, so a green `main`
covers it. Let it run as a backstop, but do not serialize behind it. When the
branch also carries mngr/minds code, gate on it.

**The steps are dependency order, not a queue.** Once `main` is green and the
bump commit exists, `GREEN_MNGR_SHA` is fixed: cut the dwt branch (step 3) and
fire launch-to-msg (step 4) immediately, with both branches' CI running in
parallel.

## Fast-forward path (low-risk releases)

When the release carries **no functional mngr/minds code** and **no `system_interface`-facing vendor change** — a version bump paired with a vendor refresh that only moves code the binary and in-VM agent already agree on — you have high confidence launch-to-msg will pass, so you can collapse the double verification into a single run. This is the common case for a routine patch bump, and it's fast *because* the change is low-risk, not because it skips the check that matters.

The fast path drops the two pre-merge safety nets and keeps the one real gate:

- **Skip** the CI-surface PRs (step 2). The dwt PR's `test` job is fully covered by launch-to-msg end-to-end, and traditional CI on an inert bump is redundant with a green `main`.
- **Skip** the pre-merge launch-to-msg (step 4). The tag run (step 8) becomes the single end-to-end verification.
- **Keep** the vendor-match check (step 6) — a local `git ls-tree` blob-hash comparison, not CI. It's the one thing that catches a bad archive before you tag, and it costs nothing.

Sequence:

1. **Bump** version + `FALLBACK_BRANCH` (step 1) on a short mngr branch; `GREEN_MNGR_SHA` = its HEAD.
2. **Sync** dwt `system/vendor/mngr` from `GREEN_MNGR_SHA` (step 3) on a short dwt branch.
3. **Land both on `main` by fast-forward** — `git push origin <branch>:main` in each repo. When `main` hasn't moved since you cut the branch (the usual case for a quick release), this is a clean FF and `main` HEAD *becomes* the exact SHA you tag: mngr `main` = `GREEN_MNGR_SHA`, dwt `main` = the vendor-sync commit. No merge commit; the branches' commits landing on `main` auto-close any PR you happened to open. *If `main` did advance, the FF is rejected — fall back to a `--no-ff` merge commit and still tag `GREEN_MNGR_SHA` (the merge parent), never `main` HEAD (see steps 6-7).*
4. **Verify vendor-match** against the post-merge `origin/main` (step 6). This is the gate — do not tag on a mismatch.
5. **Tag** both at the frozen SHAs (step 7): mngr at `GREEN_MNGR_SHA`, dwt at its post-merge `origin/main`.
6. **launch-to-msg once, on the tags** (step 8): `commit_sha=minds-v<version>`, `template_ref=minds-v<version>`.
7. **Green concludes the artifacts** — verify the Slack round-trip message. The fast path collapses the *CI* verification; it does not skip the staging rehearsal, which is the gate a low-risk bump is least likely to trip and most likely to be waved through.

**Tradeoff.** You merge and tag *before* the single verification, so the tag run is the first time the pair runs end-to-end. If it fails, the blast radius is small and recoverable: `main` carries only an inert version bump (no test asserts the version literal, so `main` stays green) plus a vendor refresh no consumer reads, and you fix forward onto the next version rather than moving the tag. That recoverability is why the fast path is for **low-risk releases only.** If the branch carries real mngr/minds code, a `system_interface` consumer change, or a large vendor jump, use the thorough Procedure below so the pre-merge launch-to-msg catches a break *before* anything lands on `main`.

## Procedure (thorough path)

Use this when the fast-forward path above doesn't apply. Each numbered step is also referenced by the fast path, so the step numbers are shared.

### 0. Cut the apt snapshot mirror timestamp (before any T bump lands)

Only needed when the release advances the DEFAULT_WORKSPACE_TEMPLATE
`.mngr/apt-snapshot-timestamp` (the pinned Debian archive timestamp every
workspace's apt sources resolve against). The mirror must serve the new
timestamp BEFORE the bump commit lands anywhere an image could be built from,
or fresh builds fail on missing indexes. Run the `apt-mirror` operator CLI
with the R2 credentials from the `secrets/minds/production/apt-mirror` Vault
entry exported (see `apps/apt_mirror/README.md`):

```bash
# Freeze the index set for the new timestamp (idempotent; minutes). On
# success this rewrites apps/apt_mirror/current-timestamp -- commit it.
uv run apt-mirror cut --timestamp <YYYYMMDDTHHMMSSZ>
# Pre-fetch the committed package lists' pool files (parallel; exits
# nonzero on any gap), then double-check read-only:
uv run apt-mirror warm
uv run apt-mirror verify
```

Only after the cut succeeds, commit the new timestamp to
`.mngr/apt-snapshot-timestamp` on the DEFAULT_WORKSPACE_TEMPLATE branch --
it must match the freshly committed `apps/apt_mirror/current-timestamp`.
Setting `APT_MIRROR_BASE_URL` empty in a workspace build falls back to
snapshot.debian.org at the same timestamp (correct but throttled), so a
not-yet-warmed mirror degrades to slow, never to wrong; warming only
pre-pays the read-through for the packages workspaces actually install.
Bring-up note: the very first cut ever is this same command -- see the
one-time bring-up runbook in `apps/apt_mirror/README.md`.

### 1. Bump version + FALLBACK_BRANCH (mngr branch)

For an iteration of the same version, skip. To bump: set `apps/minds/package.json` `version` (e.g. `0.3.1`) and `imbue/minds/build_info.py` `FALLBACK_BRANCH` to `"minds-v0.3.1"`. This bakes in a tag that doesn't exist until step 7 — fine, because step 4 overrides the DEFAULT_WORKSPACE_TEMPLATE ref via `template_ref`, so the tag is only hit in step 8. That forward reference is load-bearing: a pointer naming a dwt *SHA* has no fixed point, since `build_info.py` is itself inside the tree vendored into dwt, so committing the SHA changes the content the SHA is derived from.

**The bump commit is also what makes concurrent cuts safe.** Two cuts read the same version from `main`, both push a bump, and git rejects the second as non-fast-forward; it re-reads and takes the next number, so a collision surfaces here rather than at tag time. A failed cut therefore **burns its version** — never reuse one, or two cuts can converge on the same number. Gaps are harmless; after a burn `main`'s `package.json` names the last *attempted* cut, so "what is the current release" means the latest tag.

Also maintain the connector's wire-compat snapshot corpus (`apps/remote_service_connector/imbue/remote_service_connector/compat/`):

- **Append** a snapshot module for the release being cut (`wire_models_minds_<version>.py`, registered in `wire_compat_test.py`'s `_SNAPSHOTS`): a self-contained copy of the release's strict-parsed connector response models, stamped with `RELEASE_DATE` and a `SUPPORT_ENDS` of release date + the support window (~1 month today). While every client model is a tolerant `WireModel`, consecutive releases usually share a snapshot — only add a new module when the strictly-parsed surface actually changed; otherwise extend the newest snapshot's `SUPPORT_ENDS` to cover the new release.

- **Prune** any snapshot whose `SUPPORT_ENDS` has passed (the compat test fails loudly until you do), after confirming via the connector access log's `imbue_client` field that no in-window clients of that release remain. Pruning is what un-freezes the response shapes that snapshot pins; also remove any server-side compat shims whose `CLEANUP` note keys off that release.

### 2. Traditional CI on both branches (parallel, not a serial gate)

`ci.yml` runs only on PRs (any branch) and on push to `main` — **a bare branch push triggers nothing**, so open a branch as a PR when you want its CI. Gate on the **DEFAULT_WORKSPACE_TEMPLATE** PR's `test` job (`uv sync --all-packages` + root/`system_interface` pytest — exactly what a bad vendor refresh trips). The **mngr** branch's suites (`test-offload`, `test-docker`, `test-offload-acceptance`) are real signal only if it carries mngr/minds code; for a version-bump-only branch they're redundant with a green `main` (see "What actually gates a release"), so a PR there is optional. The release SHA — `GREEN_MNGR_SHA` — is the mngr release-branch HEAD (`main` + the bump commit) and doesn't depend on any of this finishing.

### 3. Refresh DEFAULT_WORKSPACE_TEMPLATE `system/vendor/mngr` from the green mngr SHA (DEFAULT_WORKSPACE_TEMPLATE branch)

On the DEFAULT_WORKSPACE_TEMPLATE release branch (cut from `origin/main`, clean tree), with the **mngr checkout positioned at `GREEN_MNGR_SHA`** (the mngr release-branch HEAD), run the sync recipe. You can do this the moment the bump commit exists — no need to wait for step 2's CI.

`just sync-vendor-mngr` reads `DEFAULT_WORKSPACE_TEMPLATE_DIR` from your `apps/minds/.env` (Session setup) — no path is baked into the justfile. It does `git archive HEAD` → DEFAULT_WORKSPACE_TEMPLATE `system/vendor/mngr` (tracked files only; keep `apps/minds/`), regenerates DEFAULT_WORKSPACE_TEMPLATE's root `uv.lock`, commits both as `Sync system/vendor/mngr to <branch> (<short>)`, aborts if DEFAULT_WORKSPACE_TEMPLATE is dirty, and **does not push** — it prints the exact `cd … && git push` line (with the resolved DEFAULT_WORKSPACE_TEMPLATE path) for you to run. For why releases use `git archive` (vs the dev loop's `rsync`), see `apps/minds/docs/vendor-mngr-sync.md`.

```bash
just sync-vendor-mngr                       # reads DEFAULT_WORKSPACE_TEMPLATE_DIR from .env
# (or pass the path explicitly: just sync-vendor-mngr /abs/path/to/default-workspace-template)
# then copy the `To publish: (cd <default_workspace_template> && git push origin <branch>)` line the recipe
# printed (it already has the resolved absolute path) and run it verbatim
```

If the new vendor changes an mngr API a consumer calls (e.g. `system_interface`), fix that consumer in this same branch (its own commit, so it stays reviewable).

### 4. Prove the pair green pre-merge

This is the long pole — fire it as soon as the DEFAULT_WORKSPACE_TEMPLATE branch exists, in parallel with both branches' traditional CI. The tag doesn't exist yet, so pass the DEFAULT_WORKSPACE_TEMPLATE release branch as `template_ref`. `commit_sha` and that branch's `system/vendor/mngr` must be the same mngr SHA.

```bash
GREEN_MNGR_SHA=<mngr release-branch HEAD: main + the bump commit>   # carried through to steps 6-8
cd "$MNGR"
gh workflow run minds-launch-to-msg.yml -R imbue-ai/mngr-internal \
  -r <mngr-release-branch> -f commit_sha="$GREEN_MNGR_SHA" -f template_ref=<default-workspace-template-release-branch>
```

`build` packages/reuses (keyed by `commit_sha`) the bundle; `launch_to_msg` launches it, creates an agent from the DEFAULT_WORKSPACE_TEMPLATE ref, sends a first message, asserts the round-trip. Invoke from the mngr cwd — from the DEFAULT_WORKSPACE_TEMPLATE cwd it has 404'd mid-create and duplicated the run.

Both inputs accept a full 40-char SHA, branch, or tag, and are **frozen to SHAs at run start**: the run builds the frozen mngr SHA and creates the agent from the frozen DEFAULT_WORKSPACE_TEMPLATE SHA, so pushing more commits to either branch after dispatch does nothing to an in-flight run — re-dispatch to pick them up. The slack message and step summaries report `ref (sha)`; those SHAs are exactly what ran. Passing `$GREEN_MNGR_SHA` and the DEFAULT_WORKSPACE_TEMPLATE branch's current SHA directly (instead of branch names) makes the pin explicit and replayable.

### 5. Review real code only (if any)

The version bump and the `system/vendor/mngr` refresh need no review (see "The two release branches"). The only thing to read is reviewable code that rode along — mngr/minds code on the mngr branch, or a `system_interface` fix on the DEFAULT_WORKSPACE_TEMPLATE branch. With `main` unprotected, even that review is social, not a gate. Nothing is tagged yet.

### 6. Land both branches on `main`

With `main` unprotected you can merge locally (`git merge --no-ff <branch>`, then push) or via a PR — either works. **Land the mngr branch with a merge commit, never a squash.** `main` can advance past the SHA you built and verified in step 4 (`$GREEN_MNGR_SHA`) while you were verifying; a merge commit keeps that exact SHA reachable on `main` as a parent (a squash replaces it with a new commit whose tree also contains the drift — and the binary you verified was built from neither).

The tag pins **`$GREEN_MNGR_SHA`** — the SHA the binary was built from and DEFAULT_WORKSPACE_TEMPLATE's `system/vendor/mngr` was archived from — **not** `main`'s HEAD. Confirm the *commit you'll actually tag* (DEFAULT_WORKSPACE_TEMPLATE `origin/main` post-merge, not your local working copy) still matches that SHA:

Compare the two git **trees** by `(blob-hash, path)` — content-exact, and immune to the symlinks, file modes, and `.gitignore` drops that make `diff -r` on extracted tarballs noisy. The only expected delta is files DEFAULT_WORKSPACE_TEMPLATE's `**/.minds/` ignore strips on `git add` (Vault policies + deploy scripts — not part of the installed mngr package); **anything else, especially under `system/vendor/mngr/libs/**`, is a real mismatch.**

```bash
GREEN_MNGR_SHA=<the SHA from step 4>
git -C "$DEFAULT_WORKSPACE_TEMPLATE" fetch origin --quiet
real_diff=$(diff \
  <(git -C "$MNGR" ls-tree -r "$GREEN_MNGR_SHA"        | awk '{print $3, $4}' | sort) \
  <(git -C "$DEFAULT_WORKSPACE_TEMPLATE"  ls-tree -r origin/main:system/vendor/mngr  | awk '{print $3, $4}' | sort) \
  | grep '^[<>]' | grep -v '\.minds/')
[ -z "$real_diff" ] \
  && echo "OK: system/vendor/mngr == mngr $GREEN_MNGR_SHA (modulo .minds/)" \
  || { echo "MISMATCH — re-run step 3 / re-merge DEFAULT_WORKSPACE_TEMPLATE:"; echo "$real_diff"; }
```

Comparing the mngr side against `main` (HEAD) instead of `$GREEN_MNGR_SHA` may surface extra differences — that's **expected drift** (unrelated commits landed on mngr `main` after you built), not an error. Always compare against, and tag, `$GREEN_MNGR_SHA`.

> **This check assumes `system/vendor/mngr` is the full `git archive` of the mngr SHA.** The `mngr/vendor-public-subset` branch changes `just sync-vendor-mngr` to materialize only the *public subset* (default-workspace-template is a public repo) without updating this comparison. Under that vendoring the command reports ~1,700 unfilterable `<` lines by construction — every excluded path, plus content differences in files whose `BEGIN-INTERNAL` blocks are stripped — so the release's only local gate becomes noise. If that branch has landed and this check has not been rewritten to compare against a materialized subset, treat the gate as absent and say so rather than waving a red result through.

### 7. Tag the verified pair — *not* `main` HEAD

Tag mngr at **`$GREEN_MNGR_SHA`** (the built+verified SHA; reachable on `main` as the merge parent) and DEFAULT_WORKSPACE_TEMPLATE at the commit whose `system/vendor/mngr` is that SHA's archive (the DEFAULT_WORKSPACE_TEMPLATE branch's merge into `main`):

```bash
# $GH_TOKEN, $MNGR, $DEFAULT_WORKSPACE_TEMPLATE from Session setup
VERSION=minds-v0.3.1
GREEN_MNGR_SHA=<the SHA from step 4>
git -C "$DEFAULT_WORKSPACE_TEMPLATE" fetch origin --quiet; DEFAULT_WORKSPACE_TEMPLATE_SHA=$(git -C "$DEFAULT_WORKSPACE_TEMPLATE" rev-parse origin/main)   # system/vendor/mngr == archive $GREEN_MNGR_SHA (verified in step 6)

git -C "$MNGR" tag -a "$VERSION" "$GREEN_MNGR_SHA" -m "minds $VERSION: mngr $(git -C "$MNGR" rev-parse --short $GREEN_MNGR_SHA) / DEFAULT_WORKSPACE_TEMPLATE $(git -C "$DEFAULT_WORKSPACE_TEMPLATE" rev-parse --short $DEFAULT_WORKSPACE_TEMPLATE_SHA) (system/vendor/mngr from mngr $GREEN_MNGR_SHA)"
git -C "$MNGR" push https://x-access-token:$GH_TOKEN@github.com/imbue-ai/mngr-internal.git refs/tags/"$VERSION"

git -C "$DEFAULT_WORKSPACE_TEMPLATE" tag -a "$VERSION" "$DEFAULT_WORKSPACE_TEMPLATE_SHA" -m "minds $VERSION: DEFAULT_WORKSPACE_TEMPLATE $(git -C "$DEFAULT_WORKSPACE_TEMPLATE" rev-parse --short $DEFAULT_WORKSPACE_TEMPLATE_SHA) / mngr $(git -C "$MNGR" rev-parse --short $GREEN_MNGR_SHA) (system/vendor/mngr from mngr $GREEN_MNGR_SHA)"
git -C "$DEFAULT_WORKSPACE_TEMPLATE" push https://x-access-token:$GH_TOKEN@github.com/imbue-ai/default-workspace-template.git refs/tags/"$VERSION"
```

Tags must be annotated (`-a`). **Tag the verified SHA, never `main` HEAD** — between step 4 and the merge, `main` can pick up unrelated commits never built into the binary or run through launch-to-msg (e.g. `main` HEAD once sat +58 such files past the tagged SHA). **Tags are immutable.** Once anything has run against `minds-v<version>` — a
ToDesktop build, a pool bake, a promoted channel — do not move it; fix forward
and cut the next version. Downstream caches key on the tag *name*, so a moved tag
serves stale content while reporting success. Version numbers are free and gaps
are harmless. (Correcting a tag *nothing* has consumed yet is the one exception:
`git tag -d "$VERSION"` then `git push --force ... refs/tags/"$VERSION"`, having
confirmed no build or bake used it.)

### 8. Close the loop: CI on the two tags

Both refs = the tag, exercising the binary's baked `FALLBACK_BRANCH` end to end. Because the mngr tag is the step-4 SHA, `build` reuses the bundle you already verified:

```bash
cd "$MNGR"; VERSION=minds-v0.3.1
gh workflow run minds-launch-to-msg.yml -R imbue-ai/mngr-internal \
  -r main -f commit_sha="$VERSION" -f template_ref="$VERSION"
```

**Green here concludes the *binary*.** Note the build ID in the `build` summary. If any tier you are releasing configures a pre-baked Lima image, the release is not finished until §8b has published one for this tag and §8c has proven it — otherwise those users silently lose the fast create path.

### 8b. Publish the pre-baked Lima image (only if a tier configures one)

[../setup/lima-image.md](../setup/lima-image.md) owns this: how the image is
built, published, signed and consumed. It is per-release, operator-run, and keyed
to the binary's `FALLBACK_BRANCH`.

**It applies only to a tier whose `client.toml` sets `lima_image_base_url`, and
today none does** — so this step is currently a no-op. Confirm rather than assume,
because the failure is silent: a tier that asks for an image and finds none gets
`VERSION_UNAVAILABLE` and quietly falls back to building in-VM, taking creates
from ~45s back to ~5 minutes with nothing turning red.

```bash
grep -rn 'lima_image_base_url' apps/minds/imbue/minds/config/envs/ \
  || echo "no tier configures an image: 8b does not apply"
```

If a tier does configure one, publish for `$VERSION` per that doc and prove it:

```bash
BASE_URL=$(grep -h '^lima_image_base_url' apps/minds/imbue/minds/config/envs/<tier>/client.toml | cut -d'"' -f2)
curl -fsS "$BASE_URL/manifests/$VERSION/root.json" | python3 -m json.tool
```

It must name `$VERSION` and list an entry per shipped arch. A 404, a different
version, or a missing arch all mean clients take the slow path.

### 9. Promote a channel

Do this **last** — after the fleet is baked and internal users have exercised the
build. Promotion is what reaches everyone else, and `allowDowngrade` is false, so
a bad build cannot be recalled from installs that already took it.

Point a channel at the build in `apps/minds/release-channels.toml` and merge; CI
publishes the manifests. Every entry needs all four fields — a missing
`rollout_percentage` is not a smaller rollout but the largest one, since a
manifest declaring none is offered to everyone:

```toml
[channels.alpha]
build_id = "<the build id from step 8's `build` job summary>"
version = "0.5.0"
fallback_branch = "minds-v0.5.0"
rollout_percentage = 100
```

`version` must equal the ToDesktop build's own version and `fallback_branch` must
be exactly `minds-v<version>`, or the publish refuses. Nothing checks the build's
commit, so cross-check the id against the `build` job that ran on your tag.

For **stable** this is the dial that bounds blast radius: start narrow and widen
over days (10 → 50 → 100 is a guideline), one merged PR per step. Lowering it
later is a partial halt, not a rollback. Alpha and beta stay at 100. Promoting
stable also means bumping the connector's download fallback — see Release
channels below. Alpha and beta have no such coupling.

```bash
uv run python -m scripts.release_channel.publish \
  --app-id "$(node -e "console.log(require('./apps/minds/todesktop.js').id)")" \
  --bucket minds-update-feed-production \
  --feed-base-url https://updates.imbueminds.com --dry-run
```

The promotion PR owes a changelog entry under `apps/minds/changelog/`, or
`ci.yml`'s `check-changelog` job fails it. Merging publishes within ~35 seconds,
unattended — the `minds-release` environment has no reviewers. Confirm after:

```bash
curl -s https://updates.imbueminds.com/<channel>-mac.yml | grep -E 'version:|stagingPercentage:'
```

> **Historical note, not a step.** The first channel-capable build had to be
> Released in ToDesktop once, because installs predating the channel code read
> ToDesktop's own feed and would never have seen our manifests. That happened
> long ago; the field is on our feed, and the *Release* action now governs only
> ToDesktop's hosted download page. **Do not click it as part of a release.**
> To confirm the field really is on our feed, compare what the two feeds serve —
> they name builds independently:
>
> ```bash
> curl -s https://updates.imbueminds.com/stable-mac.yml | head -1
> curl -s https://download.todesktop.com/26032588hqdzk/latest-mac.yml | head -1
> ```

## Verifying a release in a running workspace

**This is where two lifecycles meet**, and it answers two questions at once:

1. **Is the release good?** The test list below. CI cannot substitute for it —
   `launch-to-msg` builds one clean machine and sends it one message, so it never
   sees an upgraded workspace, a share, a terminal, or latchkey.
2. **Did the pool bake serve it?** The fast-path check at the end. A create that
   merely succeeds proves nothing: on a miss the client silently re-leases with
   relaxed attributes and rebuilds, so a stale pool still produces a working
   workspace.

So it needs a baked generation at `$VERSION` to be meaningful — bake first
([pool-hosts.md](./pool-hosts.md)). Neither doc owns this section alone; the
`release-minds` skill sequences it, on staging first and then again on production.

### Put the checkout on the release commit first

The desktop runs **from your checkout**, so a stale one tests the wrong client:
the create form is prefilled from `FALLBACK_BRANCH` and asks for the *previous*
release's tag, and the binary lacks whatever this release was cut to fix.

```bash
grep -o 'minds-v[0-9.]*' apps/minds/imbue/minds/build_info.py   # must equal $VERSION
```

While `main` still equals the tag, `git merge origin/main` gets you there; after
`main` moves on, check out `minds-v<version>` detached — which the code-guardian
hook skips by construction, since it refuses to merge into a pinned checkout.

This pins the tree for the client test only. A services deploy ships the working
tree, and the server tracks `main` ([services.md](./services.md)).

```bash
just minds-start-cloud
```

`minds-start-cloud`, unlike `minds-start`, sets no `MINDS_USE_LOCAL_WORKSPACE_DEFAULTS`
/ `MINDS_WORKSPACE_*`, so the create form keeps the shipped repo URL and
`FALLBACK_BRANCH` — the identity your bake stamped. **Leave the Branch field
prefilled**: a correct prefill is itself the check that the binary asks for the
right tag, and typing it by hand masks a wrong `FALLBACK_BRANCH` (the checkout check in *Verifying a release in a running workspace*). In
advanced settings pick the **same region you baked in**; region is an exact,
never-relaxed match.

Suggested list. Replace the first item each cut with whatever this release
changed:

- [ ] The release's own headline change — for 0.5.0, no chat at boot and the
      provider chooser in place of the login modal
- [ ] Sign in, send a message, get a reply
- [ ] Open a terminal, run a command
- [ ] Share the workspace, open the share in a browser, interact with it
- [ ] Toggle the project dropdown
- [ ] latchkey
- [ ] An existing workspace from an older version still works
- [ ] That workspace's upgrade path — `update-self`, then use it again

The last two are the only checks launch-to-msg cannot cover; it builds a clean
machine every time.

Then confirm the create actually used your bake:

```bash
test -n "${MINDS_ROOT_NAME:-}" || { echo "no env activated"; exit 2; }
grep -rn 'adopted pre-baked agent\|SLOW PATH' "$HOME/.$MINDS_ROOT_NAME/logs/"
```

The log directory follows the activated tier -- `~/.minds` for production, `~/.minds-<env>` for everything else -- which is why the path is derived rather than written out. Grepping a hardcoded staging path from a production run finds the *rehearsal's* passing lines and tells you nothing.

`adopted pre-baked agent <agent-id> on leased host <host-id>` is the fast path completing. These logs accumulate across runs, so the check is not that a matching line exists -- it is that its host id appears in **this** invocation's bake report. **Do not grep
for bare `FAST PATH`** — that marker is logged when the attempt *starts*, before
the lease is requested, so it appears on a miss too. A `SLOW PATH` line means the
pool had no matching row and the container was rebuilt: the workspace works, so
the create "succeeding" proves nothing on its own.

If anything fails, the release is not ready. Fix it and **cut the next version**
rather than moving this one's tags — version numbers are free and gaps are
harmless.

> **Why not just re-point the tag?** It bit 0.4.3. The per-box image cache is
> keyed by tag **name**, not content, so every box already holding that tag's tar
> skips the seed phase and loads the old image while the bake reports N/N
> succeeded. There is no purge command — you would have to SSH each box as the
> lima user, delete
> `~/.cache/mngr-slice-default-workspace-template/*-<tag>.tar`, re-bake, and
> re-prove the content by grepping the built bundle. Taking the next number costs
> nothing and avoids all of it.

## Release channels

Clients only offer a channel beyond stable when the tier's `client.toml` sets
`update_feed_base_url`; a tier that sets none is stable-only and still auto-updates.
Production sets it (`https://updates.imbueminds.com`), so builds cut from here offer
stable and alpha; beta is in the machinery but listed for nobody until an audience
for it is decided. That URL is compiled in at build time, so installs shipped before it was
committed stay stable-only for good. See `specs/minds-release-channels/spec.md`.

### Working on the update UI

The updater does not run in dev at all: `app.isPackaged` gates it, so `pnpm start`
reports itself disabled and never checks, downloads, or installs. That is not a
limitation to work around -- an unpackaged app has no signed bundle for Squirrel
to swap, so there is nothing for a dev run to update.

| what you are working on | where |
|---|---|
| the card, or its copy | `/_dev/styleguide`, where it is catalogued with a made-up version |
| the panel, or its copy | Settings > Updates in a dev run, which renders the disabled state |
| what a channel currently serves | `curl https://updates.imbueminds.com/alpha-mac.yml` |
| checking, downloading, installing, restarting | a packaged build, and nothing else |

Frontend edits need the app restarted, not reloaded. `pnpm start` builds the SPA
in its `prestart` step, so an edit made after that is not in the bundle being
served and `Cmd-R` re-fetches the same one; restarting rebuilds it.

### One-time feed setup

Provisioning a tier's update-feed bucket and hostname is done once per
environment: [../setup/update-feed.md](../setup/update-feed.md).

### Rolling out a build gradually

Stable does not have to push to everyone at once. A new build can start at
`rollout_percentage = 10` and widen over several days -- 10% -> 50% -> 100% is a
guideline, not a rule, and nothing enforces it. One merged PR per step, each
reviewed and dry-run like any other promotion.

Each install has a UUID at `~/.minds/.updaterId`, and that UUID decides whether it
falls inside a given percentage. It is fixed for the life of the install, so a
band is nested: everyone offered a build at 10% is also offered it at 50%. This
only affects in-app updates; the stable download link always serves the latest
stable version.

**Lowering the percentage is how you stop a bad build part-way through a ramp.**
electron-updater re-reads it on every check, so a narrower band is a strictly
smaller one and whoever has not polled yet stops being offered the build. Drop to
`0` to stop it reaching anyone new. This is a partial halt, not a rollback: it
recalls nobody who already took it, because `allowDowngrade` is false and the
updater arms the install before the download finishes. To move those users you
need a *new* build, which is [withdrawing](#withdrawing-a-build).

### Withdrawing a build

Withdrawing **stable** also means lowering the connector's download fallback to
the build being rolled back to, and deploying the connector. Until that lands,
the fallback names the withdrawn build -- the one direction the promotion step
warns about, since `allowDowngrade` is false and anyone who takes it during a
feed outage stays on it.

A channel moves only by repointing its entry. **Removing an entry withdraws
nothing** — no manifest is ever deleted, so the channel keeps serving its last
build; the run names it instead of reporting a promotion.

`git revert` is the undo for either dial. Reverting a *ramp step* restores the
previous, smaller band, and reverting a version *bump* moves the channel back to
the older build. Both publish through the reviewed file like any other move, and
the dry run on the PR names a backwards version move so a reviewer sees it.

A backwards move changes what a **new download** gets. It moves nobody who
already has the newer build — `allowDowngrade` is false, so they stay there until
a release passes it. Lower the connector's download fallback in the same change,
exactly as you would for a forward stable promotion.

To publish from your machine instead of a PR — with R2 credentials in the
environment (`R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`), after
landing the file edit so the file and the bucket agree:

```bash
uv run python -m scripts.release_channel.publish \
  --app-id "$(node -e "console.log(require('./apps/minds/todesktop.js').id)")" \
  --bucket minds-update-feed-production \
  --feed-base-url https://updates.imbueminds.com --dry-run
```

Drop `--dry-run` to write.

## The public download link

`https://minds.imbue.com/download?platform=mac-arm64` is the link to hand anyone
who wants minds. Name the architecture when you share it — minds ships Apple
Silicon only. (`mac` resolves identically and is what the marketing site's
buttons use, so it is not ours to remove; `accounts.imbue.com/download` also
answers, but share the `minds` one.) It records a campaign-tagged download event
via the `imbue_attribution` cookie, so a download can be tied to the account
created later — the contract with the marketing site is
`apps/remote_service_connector/docs/attribution-cookie-contract.md`.

**Promoting stable moves the link by itself.** The connector reads the arm64
`.dmg` out of `stable-mac.yml` and caches it for a minute, rather than having a
value baked in at release time that would not reach the running service until
someone redeployed.

If that read fails it falls back to `_DEFAULT_TARGET_BY_PLATFORM` in the
connector, which is why promoting stable bumps that constant in the same PR. The
bump reaches production at the next connector deploy, so the deployed value
trails `main` — behind stable, which is the safe direction.

To check the link tracks stable:

```bash
curl -s https://updates.imbueminds.com/stable-mac.yml | grep -o 'https://[^ ]*arm64\.dmg'
curl -s -o /dev/null -D - 'https://minds.imbue.com/download?platform=mac-arm64' | grep -i location
```

Agreement is not proof the feed was read — the pin names the same build. A
*disagreement* right after a promotion is just the cache; later, it means the
feed could not be read **and** the deployed pin is stale. The connector logs
`Could not resolve the stable download link` when a read fails, and that log is
what tells a resolved redirect from a fallback one.

## Failure modes

- **`gh workflow run` creates a duplicate run.** Always invoke from the mngr cwd (step 4).
- **`mngr create` fails "Remote branch minds-v<version> not found".** The CI shallow clone runs `git fetch --depth 1 --tags origin`; if it still fails on a fresh runner, confirm the tag was pushed (step 7).
- **Renamed workflow's sidebar entry sticks.** GHA unregisters only once all its runs are deleted: `PUT .../workflows/{id}/disable`, then `DELETE .../runs/{run_id}` for each.
