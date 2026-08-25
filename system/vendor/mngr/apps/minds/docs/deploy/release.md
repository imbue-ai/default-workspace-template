# Releasing a new minds.app

A release ships three pinned artifacts that must agree:

| Artifact | Pinned where |
|---|---|
| mngr code | a `main` SHA, tagged `minds-v<version>` |
| default workspace template | the `minds-v<version>` tag on `default-workspace-template` `main` |
| `.app` bundle | a ToDesktop build keyed by that mngr SHA |

Both repos tag with the **`minds-v<version>`** prefix (e.g. `minds-v0.3.1`), namespacing minds releases from each repo's own `v<version>`. The shipped binary clones the DEFAULT_WORKSPACE_TEMPLATE tag at runtime via `FALLBACK_BRANCH` in `apps/minds/imbue/minds/build_info.py`; tag immutability pins a binary to the snapshot it was verified against.

Both repos release from **`main`**. Neither `main` is branch-protected, so a PR is **never a merge gate** — you can push or merge to `main` directly. Its only role here is as a **CI surface**: `ci.yml` runs on PRs (any branch) and on push to `main`, *never on a bare branch push*, so opening a PR is how you get traditional CI on a release branch. Nothing is opened for human *review* unless real code rides along. Each repo gets a short-lived **release branch** (mngr: the version bump; DEFAULT_WORKSPACE_TEMPLATE: the `system/vendor/mngr` refresh); you prove the pair green, land both on `main`, tag each `main`, then re-prove green against the tags. **Green CI on the tags concludes the release**; clicking *Release* in ToDesktop is an optional follow-up.

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

Set these once for the whole session — later steps assume them:

- **`GH_TOKEN`** (derived, per session) — `export GH_TOKEN=$(gh auth token --user weishi-imbue)`. Pre-flight any push with `gh api user --jq .login` → must print `weishi-imbue` (the keychain "active" account drifts between parallel agents).
- **`MNGR`** and **`DEFAULT_WORKSPACE_TEMPLATE`** — absolute paths to your `mngr` and `default-workspace-template` clones, used by the shell commands in steps 4/6/7: `export MNGR=/your/mngr DEFAULT_WORKSPACE_TEMPLATE=/your/default-workspace-template`.
- **`DEFAULT_WORKSPACE_TEMPLATE_DIR`** — the *same* `default-workspace-template` path, but consumed by `just sync-vendor-mngr` (step 3), which reads it from a gitignored `apps/minds/.env` (minds-scoped, never committed — only that recipe loads it, so no shell-rc edit and it reaches non-interactive agent shells; see `apps/minds/.env.example`):
  ```bash
  echo "DEFAULT_WORKSPACE_TEMPLATE_DIR=$DEFAULT_WORKSPACE_TEMPLATE" >> apps/minds/.env
  ```
  An agent: if `apps/minds/.env` doesn't already define `DEFAULT_WORKSPACE_TEMPLATE_DIR`, ask the user for their checkout path — don't guess.

## What actually gates a release (vs. confirmation)

Three things must hold; only two need *new* CI:

1. **The binary built from the release SHA works end-to-end** — `minds-launch-to-msg.yml` (step 4). `main` never runs this, so it is the release's only unique verification and its wall-clock long pole. Start it as early as possible.
2. **The DEFAULT_WORKSPACE_TEMPLATE PR's `test` job is green** (step 2) — real signal: it refreshes `system/vendor/mngr` (and may carry a `system_interface` fix), so a `uv`-resolution or stale-API break surfaces here. `ci.yml` only runs on a PR or on `main`, so this needs the DEFAULT_WORKSPACE_TEMPLATE branch opened as a PR (a CI surface, not a review).
3. **`system/vendor/mngr` equals the tagged mngr SHA** — proved by reproduction (the step-6 `git ls-tree` blob-hash comparison), not by CI.

*Not* new signal: **traditional CI on a version-bump-only mngr branch.** Bumping `version` + `FALLBACK_BRANCH` can't change test behavior — no test asserts the version literal or that `FALLBACK_BRANCH` resolves to an existing tag — so a green `main` already covers it. Let those jobs run as a backstop; don't serialize behind them. (When the mngr branch *also* carries mngr/minds code, its CI is real signal — gate on it.)

**So don't run the steps strictly in series.** Once `main` is green and the bump commit exists, the release SHA (`GREEN_MNGR_SHA` = mngr release-branch HEAD) is fixed: cut the DEFAULT_WORKSPACE_TEMPLATE branch (step 3) and fire launch-to-msg (step 4) right away, and let both branches' traditional CI finish in parallel. The numbering below is dependency order, not "wait for each."

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
7. **Green concludes the release** — verify the Slack round-trip message.

**Tradeoff.** You merge and tag *before* the single verification, so the tag run is the first time the pair runs end-to-end. If it fails, the blast radius is small and recoverable: `main` carries only an inert version bump (no test asserts the version literal, so `main` stays green) plus a vendor refresh no consumer reads, and the tag is re-cuttable (`git tag -d` + force-push) once you fix and re-verify. That recoverability is why the fast path is for **low-risk releases only.** If the branch carries real mngr/minds code, a `system_interface` consumer change, or a large vendor jump, use the thorough Procedure below so the pre-merge launch-to-msg catches a break *before* anything lands on `main`.

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

For an iteration of the same version, skip. To bump: set `apps/minds/package.json` `version` (e.g. `0.3.1`) and `imbue/minds/build_info.py` `FALLBACK_BRANCH` to `"minds-v0.3.1"`. This bakes in a tag that doesn't exist until step 7 — fine, because step 4 overrides the DEFAULT_WORKSPACE_TEMPLATE ref via `template_ref`, so the tag is only hit in step 8.

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

Tags must be annotated (`-a`). **Tag the verified SHA, never `main` HEAD** — between step 4 and the merge, `main` can pick up unrelated commits never built into the binary or run through launch-to-msg (e.g. `main` HEAD once sat +58 such files past the tagged SHA). To re-cut during iteration: `git tag -d "$VERSION"` then `git push --force ... refs/tags/"$VERSION"`.

### 8. Close the loop: CI on the two tags

Both refs = the tag, exercising the binary's baked `FALLBACK_BRANCH` end to end. Because the mngr tag is the step-4 SHA, `build` reuses the bundle you already verified:

```bash
cd "$MNGR"; VERSION=minds-v0.3.1
gh workflow run minds-launch-to-msg.yml -R imbue-ai/mngr-internal \
  -r main -f commit_sha="$VERSION" -f template_ref="$VERSION"
```

**Green here concludes the *binary*.** Note the build ID in the `build` summary. If any tier you are releasing configures a pre-baked Lima image, the release is not finished until §8b has published one for this tag and §8c has proven it — otherwise those users silently lose the fast create path.

### 8b. Build + publish the pre-baked Lima image

Bake + publish the pre-baked Lima VM image so local Lima creates of the default workspace boot the baked toolchain instead of building it in-VM. **Operator-run, not CI** — the R2 credentials and the minisign signing **private** key stay on your machine; only a public URL + public key are committed.

> **Whether this step is optional depends on the tier, and getting it wrong is silent.**
>
> - A tier whose `client.toml` sets **no** `lima_image_base_url` never looks for an image. Skipping this step changes nothing.
> - A tier that **does** set it asks for an image keyed to the binary's `FALLBACK_BRANCH`. If you bumped `FALLBACK_BRANCH` (step 1) and did not publish an image for the new tag, every client asks for a manifest that does not exist, gets `VERSION_UNAVAILABLE`, and **silently falls back to building in-VM** — creates quietly go from ~45s back to ~5 minutes, nothing turns red, and no one finds out until someone asks why creates got slow again.
>
> So: **if the tier you are releasing configures an image, this step is required, and §8c is how you prove you did it.**

The bake runs *with Lima itself* (the image is built by the same virtualizer that consumes it — `vz` on Apple Silicon, accelerated QEMU on Linux). What a desktop client uses is decided entirely by the per-tier `client.toml` (`lima_image_base_url` + `lima_image_minisign_public_key`); if those are unset, or no image is published for the tag/arch, the client **backs off to building in-VM** (so this whole step is safe to skip and safe to half-finish).

#### One-time environment setup (do once per environment, not per release)

Setup is one script, `scripts/r2/setup_tier.py`. It is idempotent (re-running reports what exists and changes nothing) and takes a full environment name rather than a tier, so each dev gets their own bucket and cannot overwrite another dev's image or production's: `production`, `staging`, `dev-<name>`.

The same script provisions the release-channel feed under `--kind update-feed`, in its own bucket with its own hostname and its own bucket-scoped credential — see [One-time feed setup](#one-time-feed-setup-do-once-per-environment). The steps below leave it at its default `--kind lima-images`.

1. **Provision the bucket, the custom domain, and a publish credential**, using the environment's existing Vault `cloudflare` entry:
   ```bash
   export VAULT_ADDR=https://vault-cluster-public-vault-df29b16f.9b573ab7.z1.hashicorp.cloud:8200 VAULT_NAMESPACE=admin
   for key in CLOUDFLARE_API_TOKEN CLOUDFLARE_ACCOUNT_ID CLOUDFLARE_ZONE_ID CLOUDFLARE_DOMAIN; do
     export $key=$(vault kv get -mount=secrets -field=value minds/<tier>/cloudflare/$key)
   done
   uv run python scripts/r2/setup_tier.py --env production --dry-run   # review, then drop --dry-run
   ```
   It creates `minds-lima-images-<env>`, attaches `lima-images-<env>.<domain>`, and mints an R2 token **scoped to that one bucket**, printing the `R2_*` credentials the publish step needs. The account-wide token above is only used to provision; whoever publishes only ever holds the bucket-scoped credential, which is `AccessDenied` against every other bucket.

   The custom domain is **required, in every environment including dev** — not a production nicety. The managed `r2.dev` origin is rate-limited, and a client extract pulls ~65,000 chunks: against `r2.dev` this reliably fails partway with `unexpected status code 429`, so the image never assembles and the fast path is dead. A custom domain is served through Cloudflare's CDN and is not throttled this way. Check the hostname is not behind a Cloudflare Access policy, which would answer the client with `401`.

2. **Generate the environment's minisign keypair** and store the secret key somewhere durable + private (a password manager / the operator's machine — never the repo, never CI). It is the trust anchor for code the app executes as a VM:
   ```bash
   minisign -G -W -p minds-lima-<env>.pub -s minds-lima-<env>.key   # -W: unencrypted, for non-interactive signing
   ```
3. **Commit the public values** into the tier's `config/envs/<tier>/client.toml` (the script prints them):
   ```toml
   lima_image_base_url = "https://lima-images-production.minds.example"
   lima_image_minisign_public_key = "RW...."                          # contents of minds-lima-<env>.pub line 2
   ```
   These are public (a URL + a public key), so they belong in the committed `client.toml` next to the other URLs. A dev env can set the same two keys in `~/.minds-<name>/client.toml` (the `minds-admin env deploy` writer round-trips them when present on the `ClientEnvConfig`).

#### Per-release publish

Build one arch per native host (amd64 on a KVM Linux host, arm64 on an Apple-Silicon Mac), then publish each into the tier bucket.

The bake host needs a Lima that can actually boot a VM. On macOS that is `brew install lima qemu` (Lima uses `vz`, and `qemu-img` is only for the final flatten). On Linux, Lima drives **qemu** and boots the guest via UEFI, so the system emulator alone is not enough -- it also needs the EDK2 firmware and the virtio option ROMs, which `qemu-utils` does not pull in:

```bash
# Debian/Ubuntu, amd64 bake host:
sudo apt install lima qemu-system-x86 qemu-utils ovmf ipxe-qemu
```

Without the firmware, `limactl start` dies with `could not find firmware for "x86_64"`; without the ROMs, with `failed to find romfile "efi-virtio.rom"`. `build-lima-image.sh` checks for the binaries up front and names these packages, but it cannot check the firmware itself. The bake user must also be in the `kvm` group.

Credentials are the three `R2_*` values `setup_tier.py` printed. Cloudflare's REST object API is not a usable alternative: it falls under the global `api.cloudflare.com` limit of 1200 requests per 5 minutes, and one image is roughly 65,000 chunks, so a publish cannot finish within that budget and starts returning `429` partway through. The S3 API is not rate-limited this way.

```bash
export R2_ACCOUNT_ID=...         # printed by setup_tier.py
export R2_ACCESS_KEY_ID=...      # bucket-scoped; cannot touch any other environment's bucket
export R2_SECRET_ACCESS_KEY=...

./scripts/build-lima-image.sh --default-workspace-template-ref "$VERSION"     # emits qcow2 + raw under scripts/lima_image/output-<arch>/
uv run python -m scripts.lima_image.publish \
  --version "$VERSION" --arch "$(uname -m | sed 's/arm64/aarch64/')" \
  --raw-image scripts/lima_image/output-*/mngr-lima-*.raw \
  --bucket minds-lima-images-production \
  --secret-key-file /path/to/minds-lima-production.key
```

Notes:
- **Publish for the tag the binary requests** — `--version` must equal the binary's `FALLBACK_BRANCH` (`$VERSION`). A mismatch isn't fatal (clients just back off to in-VM) but you lose the speedup.
- Re-publishing a near-identical image only uploads the changed chunks (content-addressed dedup); chunks are immutable, so this is safe to re-run.
- Both arches publish into the **same** bucket (the per-(version, arch) index + the shared chunk store), and the signed root manifest merges arch entries, so publishing arm64 after amd64 adds to the manifest rather than replacing it.
- Measure the real `desync` delta between two consecutive builds before investing further in reproducibility (the dominant residual churn is `/root/.cache/uv`, which `desync` largely dedups by content).

### 8c. Gate: prove the released tag actually has an image

**Do not skip this.** The failure mode of §8b is silence — a tier that configures an image but has none published just gets slow creates forever. This check is the only thing standing between that and a release, so run it for **every tier whose `client.toml` sets `lima_image_base_url`**, using the same `$VERSION` the binary ships as `FALLBACK_BRANCH`:

```bash
BASE_URL=$(grep -h '^lima_image_base_url' apps/minds/imbue/minds/config/envs/production/client.toml | cut -d'"' -f2)
if [ -z "$BASE_URL" ]; then
  echo "This tier configures no image, so it never looks for one: 8b and 8c do not apply."
else
  curl -fsS "$BASE_URL/manifests/$VERSION/root.json" | python3 -m json.tool
fi
```

It must print a manifest naming `$VERSION` and listing an entry per shipped arch. Anything else — a 404, a manifest for a different version, a missing arch — means clients will fall back to building in-VM, and the release is **not** done:

- **404** → nothing was published for this tag. Go back to §8b.
- **Manifest names a different `minds_version`** → you published under the wrong `--version`. It must equal `FALLBACK_BRANCH` exactly.
- **Your arch is missing from `entries`** → that arch was never baked. Users on it silently take the slow path.

A tag that has an image published under it is **immutable**: never move it and never republish different bytes under it. Clients cache on `(version, arch)` and only re-fetch when the signed manifest names a different hash, so a moved tag means the image and the code a create clones can silently disagree. Need different content? Cut a new tag.

### 9. Optional: dev verify + promote

Drive the build's ToDesktop zip (`https://dl.todesktop.com/26032588hqdzk/builds/<build_id>/mac/zip/arm64`, replaces `/Applications/Minds.app`) or the dev build through create-agent → first message. To ship it, point a channel at the build in `apps/minds/release-channels.toml` (see Release channels below); clients pick it up on their next check.

**Release the first channel-capable build in ToDesktop.** Nothing shipped so far
has the channel code -- every install in the field runs `@todesktop/runtime`
against ToDesktop's feed -- so the *Release* action is the only thing that can
reach them. Until a build carrying the code is Released there, no user ever
reads our manifests.

After that one Release the field is on our feed and channels take over, and the
action governs only ToDesktop's own hosted download page.

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

### One-time feed setup (do once per environment)

The channel manifests live in their own R2 bucket, `minds-update-feed-<env>`, separate from the Lima image store so a publish of one can never overwrite the other and the credential minted for one is `AccessDenied` against the other. Provision it with the same script, under the other kind, and the same Vault `cloudflare` entry as §8's one-time setup:

```bash
uv run python scripts/r2/setup_tier.py --env production --kind update-feed --dry-run   # review, then drop --dry-run
```

It creates the bucket, attaches `updates.<domain>` (production serves the bare name, every other environment gets `updates-<env>.<domain>`), and prints the `R2_*` credentials scoped to that one bucket.

The hostname is **permanent**. It is compiled into every binary through `client.toml`, so every build ever shipped keeps requesting that exact name — it can never be changed without stranding the installs that already carry it. That is why it needs a custom domain rather than an `r2.dev` URL, which is account- and bucket-derived. The rate-limit argument that forces one on the image store does not apply here: one small file polled every ten minutes, not ~65,000 chunks per extract.

Then:

1. **Store the printed credentials in Vault**, under the names the publish workflow reads: `minds/release/R2_UPDATE_FEED_ACCOUNT_ID`, `minds/release/R2_UPDATE_FEED_ACCESS_KEY_ID`, `minds/release/R2_UPDATE_FEED_SECRET_ACCESS_KEY`. They are reachable only from the `minds-release` environment, which is what keeps them away from PR-authored code.
2. **Commit the public value** into the tier's `config/envs/<tier>/client.toml`, once the hostname is live:
   ```toml
   update_feed_base_url = "https://updates.imbueminds.com"
   ```
   Nothing is signed here, so there is no minisign key to go with it: the manifests carry ToDesktop's own sha512 digests and the artifacts stay on ToDesktop's CDN.

Only builds cut **after** that commit can offer a channel beyond stable, because the URL is baked in at build time. Until it lands, and on any tier that sets no value, the app offers stable only and updates from ToDesktop's own feed.

### Cutting an alpha

**An alpha cut is this same procedure.** Steps 1-7 are unchanged: bump `version` +
`FALLBACK_BRANCH`, refresh dwt `system/vendor/mngr`, prove the pair green, land, tag
both repos `minds-v<version>`. Alpha differs in exactly two places:

- **Step 9 is replaced.** Instead of clicking *Release* in ToDesktop, edit
  `apps/minds/release-channels.toml` to name the build, and merge. Every channel
  is promoted this way now, stable included.
- **Steps 8b/8c and the pool bake are skipped.** Neither is a correctness gate:
  a missing Lima image makes the client build in-VM (~45s becomes ~5min), and a
  pool with no row at the tag falls back to leasing any host and rebuilding its
  container. Alpha accepts both. Beta and stable bake before promoting.

Three mechanics worth knowing, because they are not obvious:

- `FALLBACK_BRANCH` names a tag that **does not exist yet** (step 1 sets it, step 7
  creates it). That forward reference is load-bearing: a pointer naming a dwt *SHA*
  has no fixed point, because `build_info.py` is inside the tree vendored into dwt,
  so committing the SHA changes the content the SHA is derived from.
- **The bump commit is what makes concurrent cuts safe.** Two cuts read the same
  version from `main`, both push a bump, and git rejects the second as
  non-fast-forward; it re-reads and takes the next number. The collision surfaces
  at bump time, not twenty minutes later at tag time.
- **A failed cut burns its version.** Never reuse it -- reuse skips the bump commit,
  which is the arbiter, so two cuts could reuse the same number and collide at
  tagging. Gaps are harmless; after a burn, `main`'s `package.json` names the last
  *attempted* cut, so "what is the current release" means the latest tag.

**Every channel is a pointer we publish**, stable included, and **promotion is a
pull request**.
Edit `apps/minds/release-channels.toml` to name the build a channel should serve,
open a PR, and CI dry-runs every gate against it so review sees whether it would
actually publish. Merging applies it.

```toml
[channels.stable]
build_id = "260801n4rh5zv5d"
version = "0.3.11"
fallback_branch = "minds-v0.3.11"

[channels.alpha]
build_id = "260814ybsmu8m14"
version = "0.3.12"
fallback_branch = "minds-v0.3.12"
```

Installs that predate channels configure no feed host and keep reading
ToDesktop's feed, so moving `stable` here does not reach them. They roll onto
this manifest the first time they take a build that names a host -- there is no
flag day and nothing to migrate by hand.

Nothing is written unless the build has a ToDesktop manifest, the declared
version matches that build, and the move is not backwards (`--allow-rollback` to
withdraw a build). Versions must be plain `X.Y.Z`: they are stamped once at cut
so promotion stays a pointer move over the bytes that actually soaked.

`fallback_branch` must be `minds-v<version>` — the tag a build at that version
clones, since step 1 moves the version and `FALLBACK_BRANCH` together. Nothing
here can read the tag baked into the build, so leaving the previous release's tag
beside a bumped version would point the image gate below at the wrong image.

That image gate runs only on a tier whose `client.toml` sets
`lima_image_base_url`, where the image must exist for the tag on each arch the run
names — `--arch`, which defaults to `aarch64` alone, so an x86_64 image is
checked only when you ask for it (`--arch aarch64 --arch x86_64`).
Production sets no image store today, so that gate does not run there and the job
says so in its output — publish a build's image (§8b) before promoting it
regardless, on every arch you ship, or those users silently lose the fast create
path.

### Withdrawing a build

A channel moves only by repointing its entry. **Removing an entry withdraws
nothing** — no manifest is ever deleted, so the channel keeps serving its last
build; the run names it instead of reporting a promotion.

So `git revert` is the undo only between two builds carrying the *same* version,
which is the ordinary alpha case (a version is stamped once per cut, and every
build until the next cut repeats it). Reverting a version *bump* moves the channel
backwards, which is refused unless `--allow-rollback` is passed — and CI never
passes it, deliberately: a withdrawal is not something a merge should do by
accident. Run it by hand against the bucket, with R2 credentials in the
environment (`R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`), after
landing the file edit so the file and the bucket agree:

```bash
uv run python -m scripts.release_channel.publish \
  --app-id "$(node -e "console.log(require('./apps/minds/todesktop.js').id)")" \
  --bucket minds-update-feed-production \
  --feed-base-url https://updates.imbueminds.com --dry-run
```

Drop `--dry-run` to write, and add `--allow-rollback` for the backwards case.
Either way this only stops *new* installs: users who already took the withdrawn
build stay on it, because `allowDowngrade` is false.

## The public download link

`https://minds.imbue.com/download?platform=mac-arm64` is the link to hand
anyone who wants minds. It records a campaign-tagged download event (the
`imbue_attribution` cookie, so a download can be tied to the account created
later) and redirects to what the **stable channel** serves.

Name the architecture when you share it: minds ships Apple Silicon only, and
`mac-arm64` says so. `mac` resolves to exactly the same place and is what the
marketing site's buttons use -- see `apps/remote_service_connector/docs/
attribution-cookie-contract.md`, which is the contract with that site -- so it
is not ours to remove.

`accounts.imbue.com/download` answers identically, which is confusing rather
than useful: both are Modal custom domains on the same connector, and the route
happens to be reachable on either. They are not interchangeable elsewhere --
`accounts` is the sign-in surface and the origin baked into password-reset
links, `minds` is the hosted web chrome -- so share the `minds` one.

Nothing to do at release time. The connector reads the arm64 `.dmg` out of
`stable-mac.yml` and caches it briefly, so promoting stable moves the link by
itself. The target is read rather than written here because the connector
deploys on its own schedule: a value baked in during a release would not reach
the running service until somebody redeployed it.

If the manifest cannot be read the link falls back to ToDesktop's own channel
URL -- whatever was last *Released* there, which has not tracked our stable
channel since release channels landed. That failure is cached for the same
minute a success is, so an outage costs one download the fetch timeout rather
than all of them.

So the two below disagreeing is what an outage looks like, not proof the link
stopped tracking stable. The connector logs `Could not resolve the stable
download` with the reason, and that log is what tells the two apart.

To check the two agree:

```bash
curl -s https://updates.imbueminds.com/stable-mac.yml | grep -o 'https://[^ ]*arm64\.dmg'
curl -s -o /dev/null -D - 'https://minds.imbue.com/download?platform=mac-arm64' | grep -i location
```

## Failure modes worth knowing

- **`gh workflow run` creates a duplicate run.** Always invoke from the mngr cwd (step 4).
- **`mngr create` fails "Remote branch minds-v<version> not found".** The CI shallow clone runs `git fetch --depth 1 --tags origin`; if it still fails on a fresh runner, confirm the tag was pushed (step 7).
- **Renamed workflow's sidebar entry sticks.** GHA unregisters only once all its runs are deleted: `PUT .../workflows/{id}/disable`, then `DELETE .../runs/{run_id}` for each.
