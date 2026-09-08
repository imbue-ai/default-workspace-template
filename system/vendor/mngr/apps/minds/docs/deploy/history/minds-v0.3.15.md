# minds-v0.3.15 (2026-08-16): staging rehearsal, never shipped to production

Deployed to staging only (final deploy_id `20260816T192218Z`, from branch
`mngr/deploy-0-3-15` / PR #425). Superseded before production by 0.3.16 and
then 0.3.17, which carried all of its content.

## What it landed

- `accounts_base_url` in the committed staging + production `client.toml`
  (share-visitor links use the accounts hosts).
- The lima legacy-key tunnel fix: lima symlinks the legacy shared root key
  into the per-host keys dir, and `SSHInfo.known_hosts_path` is threaded
  explicitly through list JSON, discovery events, forward, latchkey, and the
  desktop client.
- Connector fix: Cloudflare R2 reports a missing bucket as error code 10007
  on HTTP 200/4xx, not just 404 -- the backup reaper now recognizes it
  (`_is_bucket_not_found_error` in `cloudflare.py`).
- Modal SSH hardening: banner-read connect retries (3x) and
  `banner_timeout=30` via pyinfra's connect kwargs, shared by all provider
  backends.
- Seven deployment-test fixes (release-tier tests never run in CI, so they
  were all unexecuted until `just minds-test-deployment`).

## Ops proven on staging (now the standard procedure)

- Box sweep order: `prep-server` -> `backfill-autostart` -> `repair-keys`.
- Deliberate box reboot: 5/5 workspaces recovered hands-free in ~4.5 min.
- Slice adoption / RSA -> Ed25519 client-key rotation verified live.

## Lessons

- The tier deploy hard-fails on ANY template-declared Vault key missing from
  a tier's leaves -- pre-validate both tiers before deploy day (the first
  0.3.15 deploy failed on a missing `MINDS_PAID_LIST_CACHE_TTL_SECONDS` and
  auto-recovered, bouncing containers).
- Modal answers long synchronous requests with a 303 attempt-token redirect;
  raw httpx callers must pass `follow_redirects=True` (client-side gap
  tracked as mngr-internal#446).
- v0.3.11 clients do not reconnect after a host machine reboot (app restart
  required) -- release-notes item carried forward.
- The staging box `21ae4720` (15.204.52.75) was found silently missing its
  second NVMe -- see the 0.3.17 entry for the OVH ticket trail. `just
  audit-boxes` exists to catch this class early.
