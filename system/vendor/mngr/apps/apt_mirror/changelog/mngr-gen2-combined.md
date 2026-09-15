Integration branch for the imbue_cloud slice-fleet generation 2 program (raw qemu slices with routed-tap networking on Debian 13 boxes, replacing lima), merged together with `main` and the three pre-cutover fix PRs: imbue-ai/mngr-internal#857 (SSH certificates from Vault, #850), #855 (DHCP placement, #849) and #856 (the upstream artifact mirror, #851). The per-change details live in this directory's constituent entries, listed below.

The mirror serves the slice fleet's pinned non-apt artifacts (`GET /artifacts/...`) and a frozen copy of Docker's apt repo, so a gen-2 prep never downloads from an upstream host.

Constituent entries: `mngr-mirror-upstream-artifacts.md`

Rollout note: the Worker must be redeployed (`just deploy-apt-mirror`) for the `artifacts/` route to serve what `minds-admin artifacts upload` stored; `artifacts verify` checks only the bucket, so the README now says to fetch one public artifact URL after a Worker change.

`UpstreamFetcherInterface` gains `is_served(url)` (an HTTP `HEAD`), so callers can probe whether the live mirror serves a path without downloading it.
