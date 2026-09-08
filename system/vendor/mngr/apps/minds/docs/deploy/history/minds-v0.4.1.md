# minds-v0.4.1 (2026-08-18): deployed to production

Production deployment of the minds-v0.4.1 release (tag pair: mngr
`c3df37b9d5`, default-workspace-template `6756ae19a`), per the standard
playbook in
[order-boxes.md](../setup/order-boxes.md).

## Deployment

- Production: deploy id `20260819T020036Z`, deployed from branch
  `mngr/deploy-0-4-1` (== `main` at `c3df37b9d5`, the release-tag commit --
  the server tracks `main`, slices carry the release tag). RECREATE strategy;
  both apps' health checks green; deployed URLs match the committed
  `production/client.toml`; generation id unchanged
  (`8372712100784ba1a5c9273f866c97f4`).
- Production configures no `lima_image_base_url`, so the pre-baked Lima image
  steps (release runbook 8b/8c) do not apply.

## New production bare-metal boxes (one per US region)

Ordered fresh for this deployment, `24sys032-us` (Intel Xeon-E 2288G, 8c/16t,
128 GB, softraid-2x960nvme, 14 slice slots each), $160 due now per box
(~$100/mo + $60 one-time setup, no promo waiver this round):

| Region label | OVH DC | server id | OVH order | address |
|---|---|---|---|---|
| US-EAST-VA | vin | 462c56c0-791e-48b1-9a3a-4b9ae3ccb162 | 8574708 | 135.148.123.23 (ns1009051.ip-135-148-123.us) |
| US-WEST-OR | hil | 946923eb-5c2f-466f-98da-5f0ad8a06cf8 | 8574710 | 51.81.185.234 (ns1010088.ip-51-81-185.us) |

Both were `admin server setup` to `ready` after delivery, then re-prepped
with `just prep-server` so they carry the fleet-standard observability
collector (the raw `setup` path predates the collector rollout and does not
install it).

## Pool bake

61 minds-v0.4.1 slices baked in total, all `available` at
`repo_branch_or_tag=minds-v0.4.1`, 0 failures:

- 2 early test slices per region in spare slots (vin box `68069cdb`, hil box
  `9ef5ab2e`) to exercise the release while the new boxes were in delivery.
- The standard full 14-slice bake per new box (+28).
- Spare-slot fills to capacity on four pre-existing boxes: `642c2c1c` (vin,
  8), `68069cdb` (vin, 7 more), `267e76bd` (hil, 8), `9ef5ab2e` (hil, 6
  more) -- all four now 14/14.

Result: 31 available at the tag in US-EAST-VA, 30 in US-WEST-OR. Fleet after
the deployment: 23 servers, 280/337 slots used.

Post-bake `just audit-boxes` over the whole fleet: all 23 boxes
tier-exclusive (1 authorized key, no foreign-tier slices), no degraded RAID
arrays, no raw swap devices, no unaudited boxes.

## Notes

- Delivery of both `24sys032-us` boxes took ~10 minutes; setup to `ready`
  ~18 minutes; a full 14-slice bake ~20-25 minutes per box (slices bake 4 at
  a time per box); a spare-slot fill on a box already seeded with the tag's
  box tar runs ~1 minute per slice.
- Concurrent bakes on distinct boxes (six bake invocations overlapping) ran
  without contention, as the runbook predicts.
- The lima hostagent logs transient `failed to listen tcp ... address
  already in use` warnings while a slice VM starts on a multi-slice box;
  these are noise -- the bake's own SSH against the same forwarded port
  succeeds and the slices report healthy.
