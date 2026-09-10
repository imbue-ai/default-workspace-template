# apt_mirror

A snapshot-pinned apt mirror at `https://apt.imbuepackages.com`, serving frozen, timestamp-pinned package universes to default-workspace-template workspaces and to the slice fleet's guest images, plus an explicit set of pinned non-apt artifacts (cloud images, gVisor, age, s5cmd, uv, onetun, the otel collector) the fleet's box preps download. For a timestamp `T`, `apt` sources pointed at `<base>/snap/<T>/debian` resolve exactly the same package versions forever; an artifact at `<base>/artifacts/<name>/<version>/<file>` keeps resolving after its upstream prunes the release.

There is one global mirror instance shared by every tier (no dev/staging/production split). It has two halves:

- **Serve path**: a Cloudflare Worker (`worker/`) bound to the `apt-mirror` R2 bucket in the production Cloudflare account. Public and unauthenticated, like any Debian mirror.
- **Admin path**: the `apt-mirror` operator CLI (this Python package), which writes to the same bucket directly over the S3 API. The R2 credentials are the authorization; there is no admin service and no admin key.

## How serving works

- `GET /snap/<T>/<archive>/dists/...` serves index files frozen verbatim at cut time from R2. Upstream Debian signatures are intact, so apt verifies them with the stock `debian-archive-keyring`; we hold no signing keys. Requests for a timestamp that was never cut get a 404.
- `GET /snap/<T>/<archive>/pool/...` serves package files from a single shared cache. On a miss the Worker reads through to the live archive (`deb.debian.org`), then to `snapshot.debian.org` at `T` for files the live archive has already dropped, streaming the file to the client while storing it in R2 in the background. Pool paths are version-unique and immutable, so one cache is correct for every `T`; responses carry `Cache-Control: immutable` and are also cached at the Cloudflare edge.
- `GET /artifacts/<name>/<version>/<subpath>` serves a pinned non-apt artifact from R2, with the same immutable caching and Range support. There is no read-through: the artifact set is explicit, and a missing object is a 404 until the operator uploads it (see "Artifacts" below).

Bucket layout: `snap/<T>/<archive>/dists/...` per cut (small), `pool/<archive>/pool/...` shared (grows only by changed packages between cuts), `artifacts/<name>/<version>/...` (immutable, one copy per pinned release). The bucket is keep-forever; every cut `T` and every uploaded artifact remains servable.

## Archives

The archives a cut freezes are defined in `imbue/apt_mirror/data_types.py` (`DEFAULT_ARCHIVES`):

- `debian` and `debian-security`: frozen from `snapshot.debian.org` at `T`; their pool files share the read-through cache above.
- `docker` (`https://download.docker.com/linux/debian`, suite `trixie`, component `stable`): the pinned Docker Engine the gen-2 slice guest images are customized with. Docker publishes no snapshot service, so a cut freezes its live indexes as of the cut (re-running `cut` at the same timestamp keeps what the first run froze), and -- because Docker's package files live under `dists/trixie/pool/` rather than a top-level `pool/` -- `warm` freezes the listed `.deb`s under the same `snap/<T>/docker/dists/...` prefix, where the dists route serves them. Nothing about the docker archive is ever fetched from upstream at install time: the guest customization points apt at `<base>/snap/<T>/docker` and writes Docker's signing key from the repo (`apps/minds_admin/.../slices/docker_apt_signing_key.py`); indexes and signatures are served verbatim, so apt verifies them exactly as it would upstream's. The Worker needs no docker-specific configuration (`UPSTREAM_BASE_BY_ARCHIVE` in `worker/wrangler.jsonc` only lists archives with pool read-through).

## Operator CLI

Run from the repo root. Credentials come from the `APT_MIRROR_R2_*` environment variables (see "Credentials" below).

```bash
# Freeze the index set for a new timestamp (idempotent; minutes). The Debian
# archives come from snapshot.debian.org at T; the docker archive from its live
# indexes as of now. On success this rewrites apps/apt_mirror/current-timestamp
# -- commit it.
uv run apt-mirror cut --timestamp 20260725T000000Z

# Pre-fetch every listed package's files into the bucket, in parallel (for the
# docker archive this is what pins its package files forever, so run it right
# after the cut). Exits nonzero if any listed package is unknown or unfetchable.
uv run apt-mirror warm

# Read-only completeness check of the current timestamp against the lists.
uv run apt-mirror verify
```

`warm` and `verify` default to the timestamp in `current-timestamp` and to every list in `package_lists/`; override with `--timestamp` and repeated `--list` flags.

- `current-timestamp` is the committed source of truth for the latest cut `T`. The dwt repo's `.mngr/apt-snapshot-timestamp` must hold the same value when a `T` bump lands there (the release runbook enforces this ordering; see `apps/minds/docs/deploy/ops/app-release.md`, step 0).
- `package_lists/*.txt` are committed lists of package specs (one per line, `#` comments) that warming covers -- what dwt workspaces actually install, not the whole Debian universe. An entry is a bare `name` (resolves to the newest version in the frozen index, which is what `apt-get install name` picks against it) or an apt-style `name=version` pin (`docker.txt` pins the engine to the version `libs/mngr_vps/.../host_setup.py` installs). Names are top-level only; dependencies are not resolved, and for the Debian archives read-through covers anything a list misses (slower first fetch, never a missing package). For the docker archive there is no read-through, so `docker.txt` must list every package the guest image installs. After changing what dwt or the guest image installs, create a fresh workspace, note any slow first-installs, and extend the list.

## Artifacts

Non-apt files the slice fleet pins -- the Debian trixie cloud image (amd64 for gen-2 slices, arm64 for desktop Lima VMs), gVisor `runsc` + shim with their `.sha512` files, age, s5cmd, the uv release tarball, onetun for each operator platform, and the OpenTelemetry Collector `.deb`s -- are mirrored under `artifacts/<name>/<version>/<subpath>` so a pruned or unreachable upstream release can never break a box prep, a repave, a relay provision, or a desktop Lima create. The Worker only serves them; the set and its digests live in the private operator tooling, `apps/minds_admin/imbue/minds_admin/slices/mirror_artifacts.py`, next to the consumers that pin them.

```bash
# Print the manifest (mirror URL, upstream URL, recorded digest per entry).
uv run minds-admin artifacts list

# Download every missing artifact from upstream, verify it against the recorded
# digest AND the upstream checksum file where one exists, and store it. Idempotent.
uv run minds-admin artifacts upload            # or --name gvisor --name age ...

# Read-only presence check; exits nonzero on any gap.
uv run minds-admin artifacts verify
```

The upload uses the same `APT_MIRROR_R2_*` credentials as this CLI. **Upload before you bump a pin**: a prep that references an artifact the mirror lacks fails with a 404 (deliberately -- there is no upstream fallback). The order for a new release is: add the manifest entry with its digest, `minds-admin artifacts upload`, then land the pin bump (the release checklist in `apps/minds/docs/deploy/ops/app-release.md` carries the reminder). Consumers re-verify every download on the box against the same recorded digest.

The bucket and the Worker are deployed separately: `artifacts verify` checks the bucket over the S3 API, so it passes even when the live Worker predates a route the consumers need (the `artifacts/` route shipped after the Worker's first deploy, and a stale Worker answers 400 for every artifact). After any Worker change, `just deploy-apt-mirror` and then fetch one artifact URL over HTTPS before relying on the mirror.

### Verifying a prep reaches no upstream host

The CI-side guard is `bare_metal_prep_test.py`'s check that the rendered gen-2 prep contains none of the upstream hostnames. The live proof is a from-scratch gen-2 dev box prep with those hosts unreachable. Note that pointing them at `127.0.0.1` in the box's `/etc/hosts` is NOT enough: the guest image customization runs inside a `virt-customize` helper VM whose DNS queries are forwarded to the box's configured nameserver, bypassing `/etc/hosts`. Sinkhole them at the resolver instead, for the duration of the test:

```bash
# on the box, as root
apt-get install -y dnsmasq
for host in cloud.debian.org download.docker.com storage.googleapis.com github.com astral.sh; do
    echo "address=/$host/127.0.0.1" >> /etc/dnsmasq.d/mngr-upstream-sinkhole.conf
done
systemctl restart dnsmasq
cp /etc/resolv.conf /etc/resolv.conf.mngr-backup && echo "nameserver 127.0.0.1" > /etc/resolv.conf
# confirm the helper VM honors the loopback nameserver before trusting the run:
#   virt-customize --network on any image, running `getent hosts github.com` -- it must fail
```

Then run `minds-admin server prep --server-id <id>` from scratch (delete the staged base image and its `.customization-sha256` marker first), bake a slice, and restore `/etc/resolv.conf` and remove the dnsmasq config afterwards. The guest customization runs with `set -e`, so any leftover upstream download fails the prep loudly; a completed prep is the proof.

## Credentials

One Vault entry, `secrets/minds/production/apt-mirror` (schema: `.minds/template/apt-mirror.sh`), holds:

- `APT_MIRROR_R2_ENDPOINT`, `APT_MIRROR_R2_BUCKET`, `APT_MIRROR_R2_ACCESS_KEY_ID`, `APT_MIRROR_R2_SECRET_ACCESS_KEY`: an R2 API token scoped to read/write on the mirror bucket. Used by the CLI.
- `APT_MIRROR_DEPLOY_CLOUDFLARE_API_TOKEN`: a Cloudflare token used only to deploy the Worker (Workers Scripts: Edit; Workers R2 Storage: Read, which wrangler needs to validate the bucket binding at deploy time; plus Workers Routes: Edit and DNS: Edit on the `imbuepackages.com` zone). Deliberately separate from the connector's token so package-registry access stays independently auditable.

Export them into your shell before running the CLI or deploying (e.g. `vault kv get`-based helpers, or a filled copy of the template file).

## Deploying the Worker

```bash
CLOUDFLARE_API_TOKEN=<APT_MIRROR_DEPLOY_CLOUDFLARE_API_TOKEN> just deploy-apt-mirror
```

The recipe runs `pnpm install --frozen-lockfile` and `wrangler deploy` in `worker/`. Deploys are manual and rare; there is no CI deploy. `worker/wrangler.jsonc` pins the Worker name (`apt-mirror`), the R2 binding, and the `apt.imbuepackages.com` custom domain.

Observability is Cloudflare's built-in Workers/R2 analytics; use `pnpm exec wrangler tail` in `worker/` for live request logs.

## Bring-up runbook (one-time)

All steps in the production Cloudflare account:

1. Create the R2 bucket `apt-mirror` (dashboard or `wrangler r2 bucket create apt-mirror`).
2. Mint an R2 API token scoped to that bucket (Object Read & Write). Note the endpoint (`https://<account-id>.r2.cloudflarestorage.com`), access key id, and secret.
3. Mint the Workers-deploy Cloudflare token: Account: Workers Scripts: Edit and Workers R2 Storage: Read, plus Zone: Workers Routes: Edit and Zone: DNS: Edit on `imbuepackages.com`.
4. Fill `.minds/template/apt-mirror.sh` into a tmp file and push it to Vault: `uv run scripts/push_vault_from_file.py production apt-mirror /tmp/apt-mirror.sh` (then `shred -u` the tmp file).
5. Deploy the Worker (`just deploy-apt-mirror`). The custom domain `apt.imbuepackages.com` is attached from `wrangler.jsonc`; Cloudflare creates the DNS record automatically.
6. Cut the committed timestamp: `uv run apt-mirror cut --timestamp $(cat apps/apt_mirror/current-timestamp)`.
7. Warm and verify: `uv run apt-mirror warm && uv run apt-mirror verify`.
8. Smoke-test from a scratch container: run `apt-get update && apt-get install -y jq` in `python:3.12-slim-trixie` with sources pointed at the mirror (the release test in `test_apt_mirror_release.py` does exactly this).

Only after this succeeds should the dwt change that defaults `APT_MIRROR_BASE_URL` to `https://apt.imbuepackages.com` land -- until then, dwt builds fall back to throttled `snapshot.debian.org` (slow but correct).

## Development

- Python (CLI + cut/warm/verify logic): `just test-quick apps/apt_mirror`.
- Worker: `cd apps/apt_mirror/worker && pnpm install && pnpm test` (vitest running inside workerd via `@cloudflare/vitest-pool-workers`, with upstream fetches mocked). CI runs these tests when `worker/` changes.
- The live end-to-end release tests (marked `release`) drive a real trixie container against `apt.imbuepackages.com` at the committed timestamp: `test_apt_mirror_release.py` here installs from the Debian archives, and `apps/minds_admin/.../slices/test_docker_mirror_release.py` downloads the pinned docker-ce-cli from the frozen docker archive with the committed signing key.
