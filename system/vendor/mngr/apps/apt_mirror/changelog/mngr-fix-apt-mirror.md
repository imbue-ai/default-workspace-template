New project: the snapshot-pinned apt mirror at `https://apt.imbuepackages.com`, extracted from the remote service connector into a standalone, single-global-instance service.

- A TypeScript Cloudflare Worker (`worker/`) serves the public routes straight from the `apt-mirror` R2 bucket: frozen `dists/` index sets per cut timestamp, and a shared immutable pool cache with read-through to deb.debian.org (then snapshot.debian.org at `T` for superseded files), edge caching, Range support on cache hits, and best-effort background cache writes. Unit-tested in workerd via `@cloudflare/vitest-pool-workers`.

- An operator CLI (`uv run apt-mirror cut|warm|verify`) replaces the connector's admin routes: it writes to R2 directly (the R2 credentials are the authorization; no admin key). `cut` freezes an index set and updates the committed `current-timestamp`; `warm` resolves the committed `package_lists/*.txt` against the cut Packages indexes for amd64+arm64 and fetches missing pool files in parallel, exiting nonzero on any gap; `verify` is the same check read-only.

- Deployment is a manual `just deploy-apt-mirror` (wrangler); credentials live in one Vault entry (`secrets/minds/production/apt-mirror`). See the README for the one-time bring-up runbook.
