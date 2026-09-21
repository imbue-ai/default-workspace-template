# Ordering bare-metal boxes

When [ops/pool-hosts.md](../ops/pool-hosts.md)'s sizing step says the fleet has
no room, this is how new capacity is bought. Ordering is rare and slow (delivery
plus provisioning is roughly half an hour), so it is separated from the bake
rather than inlined into it.

The standing default is **one `24sys03-v1-us` per US region per release**, though
recent releases have instead filled existing free slots when the audit showed
enough -- 0.4.2 and 0.4.3 both did. Decide from the live audit, and record which
you chose in the release's history entry.

| Region label (lease) | OVH datacenter | Box | RAM | Storage | Slices/box |
|---|---|---|---|---|---|
| `US-EAST-VA` | `vin` | `24sys03-v1-us` (Xeon-E 2288G, 8c/16t) | 128 GB | `softraid-2x960nvme` | 14 |
| `US-WEST-OR` | `hil` | `24sys03-v1-us` (Xeon-E 2288G, 8c/16t) | 128 GB | `softraid-2x1920nvme` | 14 |

Since 2026-09 OVH no longer stocks the 2x960 NVMe config in `hil` (its
availability reads `unknown`); the 2x1920 NVMe config is the orderable one there
($164/mo instead of $100/mo). Check `uv run minds-admin server pricing --region hil`
before assuming either.

OVH **orders** take the datacenter code (`vin` / `hil`); slice **bakes** take the
lease-region label (`US-EAST-VA` / `US-WEST-OR`). Nothing cross-checks the two.

## Which configs the tooling can use

Only **two-drive NVMe software mirrors** (`softraid-2x<size>nvme`). The gen-2
reinstall lays out a single md RAID1 disk group, and the slices are reflink
carves on that one NVMe-backed XFS partition, so 3-disk mirrors, 4-disk RAID10,
hardware RAID, SATA mirrors and hybrid SATA+NVMe pairs would all need layout
work that has never been exercised. Two guards keep this honest:

- `minds-admin server pricing` prices only 2x NVMe configs by default (and only
  configs OVH reports as orderable: `unavailable`, `comingSoon` and `unknown`
  statuses are skipped), so every row's base storage and `$/SLICE/MO` is
  something you can actually buy and prep. Pass `--any-storage` to see the rest.
- `minds-admin server order` refuses any other `--storage` code before it
  resolves credentials or builds a cart. `--allow-unsupported-storage` is the
  override for deliberate layout development, never for a production box.

## TPM 2.0 is a hardware precondition

A gen-2 box's storage volume unlocks at boot through its TPM, and OVH's catalog
says nothing about TPMs: units of one plan differ (a `24sys03-v1-us` delivered
to `hil` on 2026-09-19 had no TPM at all while its rack-mates, same board and
BIOS, had Intel PTT). The gen-2 prep checks for a usable TPM 2.0 right after its
package install and refuses the box before its storage partition is touched. When
it does, open an OVH intervention ticket asking for the firmware TPM (Intel PTT
or AMD fTPM) to be enabled in the BIOS, or for the unit to be replaced, then
re-run `just server-setup <id>` (it resumes at the prep).

## GAME-range plans and 1 Gbit/s ports are excluded

OVH's GAME ranges (`24risegame*`, `24sysgame*`, `24skgame*`) cannot join the
fleet, for two reasons learned from the one `24risegame022-v1-us` bought in `hil`
on 2026-09-19 (cancelled the next day):

- Their IP ships with the Game anti-DDoS profile in firewall mode with no rules,
  which drops inbound UDP that matches no rule. The management WireGuard
  (`51820/udp`) is inbound UDP, so the prep's `:22` lockdown landed while the
  overlay could never come up, and the box was unreachable by every rung of the
  operator transport. The profile cannot be removed from a GAME IP.
- They sit on a **1 Gbit/s port** (every other fleet box has a 10G port shaped to
  the 1 Gbps plan). The restore's parallel object fetch overran that port and
  collapsed: the same 12.7 GB artifact took 856 s there against 59 s on a SYS-4
  in the same datacenter.

`minds-admin server pricing` never prices a GAME plan and `server order` refuses
one outright (no override). OVH's catalog does not state port speeds, so the
second condition is checked after the fact: `server await-delivery` reads the
delivered server's `linkSpeed` and, below 10000 Mbit/s (or when OVH reports no
`linkSpeed` at all, which is refused the same way), records the box at `failed`
with its coordinates instead of `delivered`, so `setup` never reinstalls it.
Cancel such a box's renewal (`PUT /dedicated/server/<name>/serviceInfos` with
`renew.deleteAtExpiration: true`) and delete its `bare_metal_servers` row. If
you have verified the port out of band (an unreported `linkSpeed` on a box you
know sits on a 10G port), `minds-admin server set-status --server-id <id>
--status delivered` deliberately overrides the refusal, and `setup` then
proceeds; `setup` on a `failed` row prints that remedy.

`server setup` and `server prep` also refuse to finish on a box whose `:22`
lockdown went live without the WireGuard tunnel or overlay reaching it. The
row's status is left as it was (`installing` for a first `setup`); fix the UDP
path and re-run, which resumes at the prep.

## Step 2 -- preview + approve the orders (while the deploy runs)

Print OVH's **real** price preview (base + mandatory add-ons + one-time setup)
and the exact server specs for both regions **without charging**, using
`--dry-run` (builds + assigns a non-committal cart, prints the preview, then
deletes the cart -- no charge, no prompt, no DB write):

`24sys03-v1-us` has **two mandatory option families** that each offer a choice, so
both must be passed via `--option` (discovered on the first run; the command
errors and lists the offers + monthly prices until every such family is chosen):

- `--option bandwidth-1000-24sys-us` -- 1 Gbps public bandwidth, **$0/mo** (the
  paid `bandwidth-2000-24sys-us` is +$120/mo; slices don't need it).
- `--option vrack-bandwidth-500-24sys-us` -- vRack private-network bandwidth,
  **$0/mo** (we don't use vRack for slices; the paid 1000 tier is +$23/mo).

The storage differs per datacenter (see the table above), so the loop carries it
alongside the region:

```bash
for DC_STORAGE in vin:softraid-2x960nvme hil:softraid-2x1920nvme; do
  DC="${DC_STORAGE%%:*}"
  STORAGE="${DC_STORAGE#*:}"
  echo "===== ${DC} ====="
  just server-order --dry-run \
      --plan-code 24sys03-v1-us \
      --region "${DC}" \
      --memory-gb 128 \
      --storage "${STORAGE}" \
      --option bandwidth-1000-24sys-us \
      --option vrack-bandwidth-500-24sys-us
done
```

Each block prints `About to order 24sys03-v1-us in <dc>: 128GB RAM,
<storage>, 8c/16t, <usable>GB usable disk (RAID1), 1000 Mbit/s uplink -> 14
slices of 8GB` (960GB usable for vin's 2x960, 1920GB for hil's 2x1920) and an
`OVH price preview:` (subtotal / tax / due now), followed by `Dry run: cart
deleted, no order placed.` Review the price, specs, and slice count, and approve
before Step 3.

> **Expected cost:** ~$100/mo recurring for the vin box and ~$164/mo for the hil
> box (its larger 2x1920 NVMe config), plus a **~$60 one-time setup fee** each the
> first month, so budget **~$160 due now for vin and ~$224 for hil** (~$384 for
> the pair). OVH periodically runs promotions that waive the setup fee (e.g. a run
> on 2026-07-09 showed exactly $100 due now, $0 setup) -- treat any such waiver as
> a bonus, not the norm. The dry-run cart preview's "due now" is authoritative for
> what you'll actually be charged on the day; trust it over the `pricing` table's
> catalog-derived `SETUP` column.

## Step 3 -- place the orders (after approval)

Ordering does not depend on the deploy, so place both as soon as the price is
approved (the background deploy keeps running). Since you've already reviewed the
preview, use `--yes` to skip the interactive confirm:

```bash
just server-order --yes \
    --plan-code 24sys03-v1-us --region vin \
    --memory-gb 128 --storage softraid-2x960nvme \
    --option bandwidth-1000-24sys-us \
    --option vrack-bandwidth-500-24sys-us

just server-order --yes \
    --plan-code 24sys03-v1-us --region hil \
    --memory-gb 128 --storage softraid-2x1920nvme \
    --option bandwidth-1000-24sys-us \
    --option vrack-bandwidth-500-24sys-us
```

Each records a `bare_metal_servers` row at status `ordered` and echoes its
**server id**. Save both:

```bash
export SRV_VIN=<server-id-printed-for-vin>
export SRV_HIL=<server-id-printed-for-hil>
```

## Step 4 -- await delivery

Delivery for `24sys03-v1-us` is usually ~1h (the pricing table showed `~1h` /
high stock). Resumable; a no-op once delivered.

```bash
just server-await-delivery "$SRV_VIN"
just server-await-delivery "$SRV_HIL"
```

Each flips the row to `delivered` and records the serviceName + public IP.

## Step 5 -- confirm the deploy landed, then setup boxes -> ready

First make sure the background deploy from Step 1 finished cleanly (by now it will
have completed long before delivery). Do not bake against production until it has:

```bash
wait "$DEPLOY_PID" \
  && echo "deploy OK" \
  || { echo "DEPLOY FAILED -- inspect the log and re-run before continuing"; tail -n 40 /tmp/minds-deploy-${REL_VERSION}.log; }
```

Then provision both delivered boxes to `ready`.

`server-setup` reinstalls Debian with our injected SSH host key (destructive,
expected), waits for SSH, then runs the composed prep: qemu/lima/tooling, the
staged slice guest image, **and the observability collector** (production has a
boxes ingest credential in Vault, so the collector is installed and its
`otelcol-contrib` unit verified active -- a failed collector fails the setup
and the box is NOT marked ready). Resumable via status.

```bash
just server-setup "$SRV_VIN"
just server-setup "$SRV_HIL"
```

Both end at status `ready`. Confirm:

```bash
just server-list
```

You should see both new boxes `ready`, plan `24sys03-v1-us`, 14 slots each, in
their regions.
