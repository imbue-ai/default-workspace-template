# minds-v0.4.2 (2026-08-25): deployed to staging and production

Deployment of the minds-v0.4.2 release (tag pair: mngr `0e01091fae`,
default-workspace-template `cd9f1ec59`) to staging (morning) and production
(afternoon), both from branch `mngr/deploy-0-4-2`.

## Deployment

- Staging: deploy id `20260825T134133Z`, deployed from branch
  `mngr/deploy-0-4-2` (== `main` at `7147656c56`, ahead of the release tag as
  usual -- the server tracks the branch, slices carry the release tag).
  Pool-hosts migrations 028 (`028_signup_ip_hardening.sql`), 029
  (`029_transition_ownership.sql`), and 030 (`030_account_suspension.sql`)
  applied; RECREATE strategy; tier generation id unchanged
  (`cb4f80253aff47ce820c8343ac37dc60`); both apps' health checks green;
  deployed URLs match the committed `staging/client.toml`.

## Relay fleet redeploy (suspension live-tunnel kill)

All 4 staging relays redeployed AFTER the connector deploy (the ordering
rule from next_deploy.md: the old connector would fail-closed reject the
new `Ping` plugin op), re-rendering each `frps.toml`:

| region | instance | ip | relay_id |
|---|---|---|---|
| us1 | share-relay-staging-us1-1 | 147.135.77.186 | relay-5fbae0971e338e42 |
| us1 | share-relay-staging-us1-2 | 40.160.69.228 | relay-4903356bde75c4f7 |
| us2 | share-relay-staging-us2-1 | 15.204.77.207 | relay-b6f229da73f23eae |
| us2 | share-relay-staging-us2-2 | 15.204.79.37 | relay-61499251566dc5c6 |

All four `healthy` with zero probe failures after the redeploy, so the ~10s
live-tunnel kill for suspend/unshare is active on staging. The
share/suspend end-to-end verification passed later the same day (see
"Post-deploy verification" below).

## Post-deploy verification

- Signup IP hardening (migration 028): a probe of
  `https://accounts.imbue-staging.com/login` logged the caller's real
  public IP as `client_ip` in the structured access-log line, confirming
  the socket-peer derivation behind the custom domains.
- Desktop fast path: the 0.4.2 desktop client (source run,
  `just minds-start-cloud`, staging activated) leased a baked minds-v0.4.2
  slice and the workspace worked end to end (operator-tested), consuming
  the US-WEST-OR slice; a second account later consumed the US-EAST-VA one.
- Suspension live-tunnel kill (migration 030 + relay redeploys), end to
  end with a throwaway account (`thejash+bad@gmail.com`): workspace
  created from a fresh env root and shared; `minds-admin account suspend`
  revoked 2 sessions, force-stopped the workspace, downgraded the R2 key,
  and suspended the share -- the shared URL and everything else stopped;
  `unsuspend` restored the key and share, sign-in worked again, and after
  starting the workspace the share came back without re-sharing.
- Found while testing (filed as mngr-internal#607, deferred): a
  signed-out account's workspaces remain on the minds home list --
  discovery never retracts a removed provider instance's members, and the
  workspace list has no account scoping.

## Pool bake

Baked 1 minds-v0.4.2 slice per region onto the existing 0.4.0-era staging
boxes (no new boxes for staging, per the playbook), pinned by
`--server-id`:

- US-EAST-VA on vin box `72dd8187` (135.148.34.235):
  `slice-96e46e8ad30e456aa982b307bf19e546`
  (host-f83ab4874cbd416daf59a66c930f8ee0).
- US-WEST-OR on hil box `c7793839` (51.81.185.232):
  `slice-0a1427348048416080bcc010a4d35069`
  (host-97ad48ba909f4a69a1c4db07bc2bf9e9).

2/2 bakes succeeded, both rows `available` at
`repo_branch_or_tag=minds-v0.4.2` (8 GB / 2 vCPU). Pre-existing rows (19
available at minds-v0.4.1, 4 leased at older tags) untouched.

Both seed slices were consumed by the day's desktop testing (leased and
destroyed), so after the verifications a top-up bake added 4 more slices
per region onto the same boxes (8/8 succeeded, warm re-bakes -- the tag's
box tar was already seeded). Final state: 4 available minds-v0.4.2 slices
per region, both 24sys032-us boxes full at 14/14, the 19 minds-v0.4.1
slices retained for not-yet-updated clients, 3 free slots left on the old
6-slot box. Fleet after: 3 servers, 31/34 slots used.

## Production deployment (same day, afternoon)

- Pre-deploy check: the production Bugsink instance's ingress gates
  re-verified green (login 404 / ingest GET 405 / origin unreachable
  direct), and the `production/sentry` Vault entry complete (RSC + LiteLLM
  DSNs populated; `OAUTH_REDIRECTOR_SENTRY_DSN` empty by design).
- Production: deploy id `20260825T171504Z`, deployed from branch
  `mngr/deploy-0-4-2` (`main` + this deployment's docs + the updated policy
  pages). Migrations 028-030 applied; RECREATE; generation id unchanged
  (`8372712100784ba1a5c9273f866c97f4`); both apps healthy; URLs match the
  committed `production/client.toml`. The `sentry-production-<deploy-id>`
  Modal Secret was stamped for the first time, activating error reporting.
- A second deploy (`20260825T173747Z`, ROLLOVER -- no new migrations) was
  run immediately after because the first deploy's frontend build raced the
  autofix typo-fix commits to the policy pages: production briefly served
  the pre-fix wording; the re-deploy published the corrected pages
  (verified live on `accounts.imbue.com`).
- All 4 production relays (us1-1/us1-2/us2-1/us2-2, per the 0.3.17 table)
  redeployed AFTER the connector deploy; all healthy with zero probe
  failures -- the ~10s live-tunnel kill is active on production.
- Signup IP hardening: probes of `https://accounts.imbue.com/login` logged
  the caller's real public IP as `client_ip`; the captured log window also
  showed real user traffic and the relays' `/frps/auth` Ping callbacks
  attributed to their public IPs.
- Bugsink reporting verification (the item deliberately deferred at the
  instance bring-up on 2026-08-20): an invalid-model request through the
  production LiteLLM proxy answered 400 and the BadRequestError event
  landed in the production `llm` project (issues LLM-4/LLM-5) via the
  tunneled REST API (`/api/canonical/0/issues/?project=2`), proving the
  native sentry callback end to end on production.
- Desktop release-channel promotion DONE the same evening (PR #602,
  merged to `main` and into this branch): `release-channels.toml` points
  stable, beta, and alpha at the 0.4.2 build `260825un55i8ix7`.

## Production pool bake (same day, evening)

Decision: fill the fleet's existing free slots instead of ordering the
playbook's two new `24sys032-us` boxes (capacity was ample: 83 DB-surveyed
free slots across the 23 boxes). Per-box bakes pinned by `--server-id`,
4 boxes concurrent per region, both regions in parallel:

- US-EAST-VA: 27/27 succeeded across 7 boxes, 0 failures (~42 min
  wall-clock).
- US-WEST-OR: 55/56 succeeded across 14 boxes (~54 min). The one
  "failure" was a phantom slot: box `bab2c8a1` (51.81.242.232) surveyed
  3 free in the DB-derived slot accounting (which counts only this env's
  pool rows) but had only 2 actually-empty slots on-box, so 2 of its 3
  bakes succeeded, the box hit 14/14, and the third slice was refused
  with `MNGR_SLICE_BOX_FULL` and rolled back cleanly.

Result: 82 new `available` rows at `repo_branch_or_tag=minds-v0.4.2`
(27 US-EAST-VA, 55 US-WEST-OR); prior available capacity untouched
(0.3.17: 28 east / 19 west; 0.4.1: 19 east / 6 west). Fleet after:
23 servers, 337/337 slots used, 0 free -- retiring old-tag slices is the
lever for future capacity.

## Notes

- The vault token layout used per-key subpaths
  (`secrets/minds/staging/<service>/<KEY>`, field `value`) -- a flat
  `vault kv get -field=<KEY> minds/staging/relay-ssh` does not resolve.
  The `-field=value` read strips the SSH key's trailing newline, which
  ssh-add rejects (same gotcha as the 0.3.17 bring-up); re-append it.
