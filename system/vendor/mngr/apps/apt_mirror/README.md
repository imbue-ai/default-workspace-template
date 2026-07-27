# apt_mirror

A snapshot-pinned Debian apt mirror at `https://apt.imbuepackages.com`, serving frozen, timestamp-pinned package universes to default-workspace-template workspaces. For a timestamp `T`, `apt` sources pointed at `<base>/snap/<T>/debian` resolve exactly the same package versions forever.

There is one global mirror instance shared by every tier (no dev/staging/production split). It has two halves:

- **Serve path**: a Cloudflare Worker (`worker/`) bound to the `apt-mirror` R2 bucket in the production Cloudflare account. Public and unauthenticated, like any Debian mirror.
- **Admin path**: the `apt-mirror` operator CLI (this Python package), which writes to the same bucket directly over the S3 API. The R2 credentials are the authorization; there is no admin service and no admin key.

## How serving works

- `GET /snap/<T>/<archive>/dists/...` serves index files frozen verbatim at cut time from R2. Upstream Debian signatures are intact, so apt verifies them with the stock `debian-archive-keyring`; we hold no signing keys. Requests for a timestamp that was never cut get a 404.
- `GET /snap/<T>/<archive>/pool/...` serves package files from a single shared cache. On a miss the Worker reads through to the live archive (`deb.debian.org`), then to `snapshot.debian.org` at `T` for files the live archive has already dropped, streaming the file to the client while storing it in R2 in the background. Pool paths are version-unique and immutable, so one cache is correct for every `T`; responses carry `Cache-Control: immutable` and are also cached at the Cloudflare edge.

Bucket layout: `snap/<T>/<archive>/dists/...` per cut (small), `pool/<archive>/pool/...` shared (grows only by changed packages between cuts). The bucket is keep-forever; every cut `T` remains servable.

## Operator CLI

Run from the repo root. Credentials come from the `APT_MIRROR_R2_*` environment variables (see "Credentials" below).

```bash
# Freeze the index set for a new timestamp (idempotent; minutes).
# On success this rewrites apps/apt_mirror/current-timestamp -- commit it.
uv run apt-mirror cut --timestamp 20260725T000000Z

# Pre-fetch every listed package's pool files into the cache, in parallel.
# Exits nonzero if any listed package is unknown or unfetchable.
uv run apt-mirror warm

# Read-only completeness check of the current timestamp against the lists.
uv run apt-mirror verify
```

`warm` and `verify` default to the timestamp in `current-timestamp` and to every list in `package_lists/`; override with `--timestamp` and repeated `--list` flags.

- `current-timestamp` is the committed source of truth for the latest cut `T`. The dwt repo's `.mngr/apt-snapshot-timestamp` must hold the same value when a `T` bump lands there (the release runbook enforces this ordering; see `apps/minds/docs/release.md`, step 0).
- `package_lists/*.txt` are committed lists of package names (one per line, `#` comments) that warming covers -- what dwt workspaces actually install, not the whole Debian universe. Names are top-level only; dependencies are not resolved, and read-through covers anything a list misses (slower first fetch, never a missing package). After changing what dwt installs, create a fresh workspace, note any slow first-installs, and extend the list.

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
- The live end-to-end release test (`test_apt_mirror_release.py`, marked `release`) drives a real trixie container against `apt.imbuepackages.com` at the committed timestamp.
