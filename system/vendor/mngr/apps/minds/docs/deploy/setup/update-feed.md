# Update-feed setup (once per environment)

The R2 bucket and hostname serving a tier's release-channel manifests. Done once
when standing an environment up; the recurring promotion is
[../ops/app-release.md](../ops/app-release.md) step 9.

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

Reference for the channel machinery. The procedure is step 9.

**Every channel is a pointer we publish**, stable included, and moving one is a
pull request. `rollout_percentage` is required on every entry — a manifest that
declares none is offered to *everyone*, so absence is the largest rollout, not
the absence of one.

```toml
[channels.stable]
build_id = "260801n4rh5zv5d"
version = "0.3.11"
fallback_branch = "minds-v0.3.11"
rollout_percentage = 30

[channels.alpha]
build_id = "260814ybsmu8m14"
version = "0.3.12"
fallback_branch = "minds-v0.3.12"
rollout_percentage = 100
```

Installs that predate channels configure no feed host and keep reading
ToDesktop's feed, so moving `stable` here does not reach them. They roll onto
this manifest the first time they take a build that names a host -- there is no
flag day and nothing to migrate by hand.

Nothing is written unless the build has a ToDesktop manifest and the declared
version matches that build. A backwards move is not refused; it is named on the
report line. Versions must be plain `X.Y.Z`: they are stamped once at cut so
promotion stays a pointer move over the bytes that actually soaked.

`rollout_percentage` is required on every entry, and it is how much of the
channel is offered the build (see [Rolling out a build gradually](#rolling-out-a-build-gradually)).
Beta and alpha stay at `100`. It is required rather than optional because a
manifest that declares no percentage is offered to *everyone*: absence is the
largest rollout, not the absence of one, so a forgotten or misspelled field would
otherwise publish a full rollout and report it as an ordinary promotion.

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
