# minds-v0.4.0 (2026-08-18): deployed to staging

Staging-only deployment of the minds-v0.4.0 release (tag pair: mngr
`e4ffbb70ca`, default-workspace-template `7ccb7efa7`). Production deployment
is a separate, later step and is not covered by this entry.

## Deployment

- Staging: deploy id `20260818T165239Z`, deployed from branch
  `mngr/deploy-0-4-0` (== `main` at `ab51770dc1`, ahead of the release tag as
  usual -- the server tracks the branch, slices carry the release tag).
  Pool-hosts migration 027 (`027_record_format.sql`) applied; RECREATE
  strategy; tier generation id unchanged (`cb4f80253aff47ce820c8343ac37dc60`);
  both apps' health checks green; deployed URLs match the committed
  `staging/client.toml`.
- A first deploy attempt failed before touching anything user-visible
  (`modal run ... app.py::migrate_db` hit `ModuleNotFoundError: tenacity`
  because the deploy accidentally ran from a different local checkout whose
  venv was stale); the auto-`minds env recover` rolled back cleanly (apps to
  v30, Neon snapshot restore, orphan secrets GC'd) and the re-run from the
  intended checkout succeeded.

## New staging bare-metal boxes (one per US region, production box standard)

Ordered fresh for this deployment, `24sys032-us` (Intel Xeon-E 2288G, 8c/16t,
128 GB, softraid-2x960nvme, 14 slice slots each), $160 due now per box
(~$100/mo + $60 one-time setup):

| Region label | OVH DC | server id | OVH order | address |
|---|---|---|---|---|
| US-EAST-VA | vin | 72dd8187-e4b7-4d3c-a3da-8f5a1b65da98 | 8572642 | 135.148.34.235 (ns1006992.ip-135-148-34.us) |
| US-WEST-OR | hil | c7793839-5604-452f-8ada-bed05106ef49 | 8572644 | 51.81.185.232 (ns1010096.ip-51-81-185.us) |

Both were `admin server setup` to `ready` after delivery.

## Pool bake

Baked 3 minds-v0.4.0 slices per new box (`just bake-slice-prod <region>
minds-v0.4.0 3 --server-id <id>`): US-EAST-VA on the vin box, US-WEST-OR on
the hil box -- 6/6 bakes succeeded, all rows `available` at
`repo_branch_or_tag=minds-v0.4.0`. Pre-existing staging pool rows (1
available at minds-v0.3.17, 4 leased at older tags, all on box `21ae4720`)
were left untouched.

Post-bake `just audit-boxes`: both new boxes tier-exclusive (1 authorized
key, no foreign-tier slices), no degraded RAID arrays, no raw swap.

## Notes

- The old staging box `21ae4720` (15.204.52.75, hil) remains single-disk;
  OVH ticket #721263 still open (see the 0.3.17 history entry). The audit
  reports its `md2`/`md3` arrays degraded, consistent with that ticket.
- Delivery of both `24sys032-us` boxes took ~10 minutes; setup to `ready`
  ~20 minutes; each 3-slice bake ~25 minutes.
