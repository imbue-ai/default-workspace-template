# Ordering bare-metal boxes

When [ops/pool-hosts.md](../ops/pool-hosts.md)'s sizing step says the fleet has
no room, this is how new capacity is bought. Ordering is rare and slow (delivery
plus provisioning is roughly half an hour), so it is separated from the bake
rather than inlined into it.

The standing default is **one `24sys032-us` per US region per release**, though
recent releases have instead filled existing free slots when the audit showed
enough -- 0.4.2 and 0.4.3 both did. Decide from the live audit, and record which
you chose in the release's history entry.

| Region label (lease) | OVH datacenter | Box | RAM | Storage | Slices/box |
|---|---|---|---|---|---|
| `US-EAST-VA` | `vin` | `24sys032-us` (Xeon-E 2288G, 8c/16t) | 128 GB | `softraid-2x960nvme` | 14 |
| `US-WEST-OR` | `hil` | `24sys032-us` (Xeon-E 2288G, 8c/16t) | 128 GB | `softraid-2x960nvme` | 14 |

OVH **orders** take the datacenter code (`vin` / `hil`); slice **bakes** take the
lease-region label (`US-EAST-VA` / `US-WEST-OR`). Nothing cross-checks the two.

## Step 2 -- preview + approve the orders (while the deploy runs)

Print OVH's **real** price preview (base + mandatory add-ons + one-time setup)
and the exact server specs for both regions **without charging**, using
`--dry-run` (builds + assigns a non-committal cart, prints the preview, then
deletes the cart -- no charge, no prompt, no DB write):

`24sys032-us` has **two mandatory option families** that each offer a choice, so
both must be passed via `--option` (discovered on the first run; the command
errors and lists the offers + monthly prices until every such family is chosen):

- `--option bandwidth-1000-24sys-us` -- 1 Gbps public bandwidth, **$0/mo** (the
  paid `bandwidth-2000-24sys-us` is +$120/mo; slices don't need it).
- `--option vrack-bandwidth-500-24sys-us` -- vRack private-network bandwidth,
  **$0/mo** (we don't use vRack for slices; the paid 1000 tier is +$23/mo).

```bash
for DC in vin hil; do
  echo "===== ${DC} ====="
  just server-order --dry-run \
      --plan-code 24sys032-us \
      --region "${DC}" \
      --memory-gb 128 \
      --storage softraid-2x960nvme \
      --option bandwidth-1000-24sys-us \
      --option vrack-bandwidth-500-24sys-us
done
```

Each block prints `About to order 24sys032-us in <dc>: 128GB RAM,
softraid-2x960nvme, 8c/16t, 960GB usable disk (RAID1) -> 14 slices of 8GB` and an
`OVH price preview:` (subtotal / tax / due now), followed by `Dry run: cart
deleted, no order placed.` Review the price, specs, and slice count, and approve
before Step 3.

> **Expected cost:** ~$100/mo recurring per box, plus a **~$60 one-time setup fee**
> the first month, so budget **~$160 due now per box** (~$320 for the pair). OVH
> periodically runs promotions that waive the setup fee (e.g. a run on 2026-07-09
> showed exactly $100 due now, $0 setup) -- treat any such waiver as a bonus, not
> the norm. The dry-run cart preview's "due now" is authoritative for what you'll
> actually be charged on the day; trust it over the `pricing` table's
> catalog-derived `SETUP` column.

## Step 3 -- place the orders (after approval)

Ordering does not depend on the deploy, so place both as soon as the price is
approved (the background deploy keeps running). Since you've already reviewed the
preview, use `--yes` to skip the interactive confirm:

```bash
just server-order --yes \
    --plan-code 24sys032-us --region vin \
    --memory-gb 128 --storage softraid-2x960nvme \
    --option bandwidth-1000-24sys-us \
    --option vrack-bandwidth-500-24sys-us

just server-order --yes \
    --plan-code 24sys032-us --region hil \
    --memory-gb 128 --storage softraid-2x960nvme \
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

Delivery for `24sys032-us` is usually ~1h (the pricing table showed `~1h` /
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

You should see both new boxes `ready`, plan `24sys032-us`, 14 slots each, in
their regions.
