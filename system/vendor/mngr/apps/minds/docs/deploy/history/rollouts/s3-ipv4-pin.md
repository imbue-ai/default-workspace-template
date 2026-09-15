# Pinning box-to-Object-Storage traffic to IPv4

Resolution of issue mngr-internal#385 ("OVH Object Storage upload from
bare-metal boxes throttled to ~6-25 MB/s") and the rollout of the fix to the
staging and production fleets on 2026-09-12. OVH support ticket #723301
(staging account) tracks the upstream defect and stays open.

## Root cause

Two factors superposed:

1. **The boxes' public uplink is QoS-capped at the plan's 1 Gbps** (confirmed
   by OVH; they lifted it 2026-09-04..09-11 for testing, which produced the
   temporary 400-728 MB/s readings, then reverted it). Ceiling: ~110 MB/s.
2. **The in-DC IPv6 path from dedicated servers to the same-DC Object Storage
   VIP intermittently blackholes TCP flows** for tens of seconds, verified in
   both `vin` and `hil` and on both the staging and production accounts. A
   blackholed flow collapses to `cwnd:1` with RTO exponential backoff,
   receives nothing from the server for 20+ seconds, and the server then
   kills the request with `RequestTimeout`. Roughly half of IPv6 flows were
   affected at any given time, in windows that shifted minute to minute --
   which is what made earlier testing look like a multipart-API limitation,
   key-affinity, bucket state, or anti-abuse throttling (all ruled out by
   direct A/B on 2026-09-12). Forced-family comparison from the boxes:
   IPv4 88-112 MB/s in 14/14 runs with zero failures; IPv6 a 3-110 MB/s
   per-flow lottery. Cross-DC paths are healthy on both families (RTT
   self-pacing keeps them under the policer). s5cmd (Go) prefers IPv6, so
   every workspace stop/start transfer rode the broken path.

Both families show ~10-14% retransmitted bytes even on healthy flows (BBR /
cubic bursting into the 1 Gbps policer at 0.06 ms RTT); only IPv6 flows
blackhole. The blackholes vanished during the window OVH had the QoS cap
lifted, so the suspected culprit is the policer implementation on the IPv6
path -- unconfirmed; OVH has the request IDs.

## The fix

`bare_metal_prep.py` installs a managed `/etc/hosts` block pinning the S3
endpoints (`s3.us-east-va.io.cloud.ovh.us`, `s3.us-west-or.io.cloud.ovh.us`)
to their IPv4 addresses, plus a `mngr-s3-ipv4-pin` systemd timer that
re-resolves the A records every minute (via `dig`, which bypasses the hosts
file) and rewrites the block atomically -- so a changed VIP heals within a
minute and a DNS outage keeps the last-known pin. Go's resolver honors
`/etc/hosts`, so s5cmd transfers ride IPv4 with no transfer-code change.
The pin is rollout-bridging: it comes out once OVH fixes the IPv6 path
(`CLEANUP` marker in `bare_metal_prep.py`).

Validated with the exact production upload shape (`cat 4G | s5cmd pipe`,
default concurrency): 107 MB/s pinned vs 30.7 MB/s over IPv6, zero failures.
A ~13 GB stop artifact now uploads in ~2 minutes instead of 10-40.

## Rollout record (2026-09-12)

Rolled out by re-running `minds-admin server prep --server-id <id>` (the
sanctioned refresh path; idempotent, does not touch running VMs):

| Fleet | Boxes | Outcome |
|---|---|---|
| staging | 3 | all prepped and verified (pin block present, timer active); end-to-end `s5cmd pipe` at 90-108 MB/s |
| production | 23 | see the driver log summary below |

The production sweep ran one box at a time with per-box verification (pin
block lines, timer active, `qemu-system` process count unchanged across the
prep) and halt-on-first-failure. Per-box prep output and the driver log were
kept in the operator session's scratchpad; the durable record is this page
plus the per-box `mngr-s3-ipv4-pin` units now visible on every box.

The production defect was confirmed before the sweep with an 8-probe
forced-family A/B from `ns1009051` (vin): IPv4 87-103 MB/s, IPv6 down to
5-16 MB/s on starved draws. Probe objects were deleted afterwards.

## What is still owed

- OVH ticket #723301: get the IPv6 path fixed upstream, then remove the pin
  machinery (grep `CLEANUP` in `bare_metal_prep.py`).
- Benchmark leftovers on the staging account: test objects in
  `mngr-bench-staging`, `mngr-bench-staging-perf`, `mngr-bench-staging-west`,
  and `mngr-bench-staging-fresh-20260912` (~40 GB, kept for OVH
  reproduction), plus `~/ovh-bench/` directories on the three staging boxes.
- Going faster than ~110 MB/s needs more provisioned bandwidth (not orderable
  for the `24sys032-us` / `24rise01-v1-us` ranges) or the planned
  content-addressed chunk dedupe (phase 2 of workspace stop/start).
