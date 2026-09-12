# Host Pool Setup

How to set up the infrastructure for the imbue-cloud-leased pool host flow.

**Note:** the CI tier's standing boxes (used by the remote-workspace release
tests) follow this same flow with a standing "infra" DB as the canonical box
registry; their runbook lives in
[`specs/remote-workspaces-in-ci.md`](../../../../specs/remote-workspaces-in-ci.md).

Pool hosts are **bare-metal slices**: lima/QEMU VMs carved on bare-metal boxes we
operate. (The boxes are currently rented from OVH, but that is an internal
implementation detail of the slice backend; other suppliers may be added later.)

## Prerequisites

- Neon PostgreSQL database (two connection strings: pooled for runtime, direct for migrations)
- One or more **bare-metal boxes** registered + prepped via the
  `minds-admin server` commands (see
  [Step 5](#step-5-bake-one-or-more-pool-hosts)). Slice baking targets an
  explicitly-chosen `ready` box.
- Bare-metal box supplier credentials (currently OVH API AK / AS / CK). These
  order the bare-metal boxes that slices run on.
- Modal account (for deploying the remote_service_connector)

## Step 1: Create the database schema

**For dev envs:** skip this step. `minds-admin env deploy` (against a dev
env) provisions a brand-new Neon project per env and applies the
schema automatically by replaying
`apps/remote_service_connector/migrations/*.sql` against the new
`host_pool` database.

**For staging / production:** apply the schema once, by hand, against
the tier's pre-provisioned `host_pool` database. Use the **direct**
(non-pooled) Neon connection string:

```bash
for f in apps/remote_service_connector/migrations/*.sql; do
    psql "$NEON_DB_DIRECT" -f "$f"
done
```

The migrations are idempotent and apply cleanly to a fresh DB or one
that already has earlier migrations applied. The `000_initial_schema.sql`
file is the canonical full schema; `001`-`003` are defensive ALTERs
that no-op when 000 already laid the table down in its final shape.

The `attributes` JSONB column carries whatever shape the operator wants
to match leases against (`repo_branch_or_tag`, `cpus`, `memory_gb`,
`gpu_count`, etc.); the connector's `/hosts/lease` endpoint matches
`attributes @> request_attributes`.

## Step 2: Bring up the tier's SSH certificate authority

Gen-2 boxes, slice VMs, and workspace containers authorize **no static
management key**: they trust the tier's SSH CA in Vault, and everything that
manages them (your `minds-admin`, the connector, the analytics collector)
presents a short-lived certificate signed by it. The CA, its signing roles, and
the connector's AppRole are terraform in the imbue-ai/vault repo
(`terraform/minds_ssh_ca.tf`); the operator steps -- read the CA public key,
commit it as `[ssh_ca] public_key` in the tier's `deploy.toml`, mint the
connector's AppRole credentials into `secrets/minds/production/ssh-ca` -- are
in [setup/vault.md](setup/vault.md#ssh-certificate-authority-gen-2-management-ssh).
Without the committed `[ssh_ca]` block, gen-2 `server prep` / `setup` and
gen-2 slice bakes refuse.

Gen-1 boxes (the pre-cutover fleet) still authorize the tier's static pool key,
`secrets/minds/production/pool-ssh/POOL_SSH_PRIVATE_KEY` (generate it with
`ssh-keygen -t ed25519 -N ""` and push the private half per step 3). That key
and its Vault entry go away with the last gen-1 box.

## Step 3: Populate the tier's Vault entries

Secrets live in HCP Vault now (not `.minds/<env>/` shell files); see
`apps/minds/docs/deploy/setup/vault.md` for prerequisites. For the host-pool
flow specifically:

### secrets/minds/production/neon

The **pooled** Neon connection string:

Each key is its own single-`value` leaf (see `setup/vault.md` for the split
layout):

```bash
vault kv put -mount=secrets minds/production/neon/DATABASE_URL \
    value=postgresql://user:pass@host-pooler.neon.tech/db?sslmode=require
```

### secrets/minds/production/pool-ssh

The gen-1 management private key (not needed for a gen-2-only tier):

```bash
vault kv put -mount=secrets minds/production/pool-ssh/POOL_SSH_PRIVATE_KEY \
    value=@.minds/production/pool_management_key/id_ed25519
```

(`@<path>` tells `vault kv put` to read the value from the file -- the
file itself never leaves the operator's laptop.)

### secrets/minds/<tier>/ovh

The shared per-tier bare-metal box supplier credentials (currently OVH
AK / AS / CK). The `minds-admin server` commands (order + manage the
bare-metal boxes that slices run on) resolve them from this entry
automatically when the tier is activated; exporting the `OVH_*` env vars
remains the non-activated one-off override. NOT pushed to Modal and NOT
read by `minds-admin env deploy` / `destroy` -- no deployed service makes
supplier API calls at runtime.

Generate the trio once per tier at
<https://api.us.ovhcloud.com/createApp> (endpoint `ovh-us`; pick
whichever endpoint matches the boxes' region). Use a copy of
`.minds/template/ovh.sh` to capture the three values, then push to
Vault:

```bash
cp .minds/template/ovh.sh /tmp/production-ovh.sh
$EDITOR /tmp/production-ovh.sh
uv run scripts/push_vault_from_file.py production ovh /tmp/production-ovh.sh
shred -u /tmp/production-ovh.sh
```

The same steps work verbatim for `staging` and `dev` (substitute the
tier in the path). Dev-tier credentials are shared across all
per-developer dev envs.

## Step 4: Push the Vault changes to Modal and redeploy

```bash
eval "$(uv run minds-admin env activate --deploy production)"
uv run minds-admin env deploy --yes-i-mean-production
```

`minds-admin env deploy` pushes every tier secret from Vault into Modal
Secrets (`<service>-production` for every service named in
`apps/minds/imbue/minds/config/envs/production/deploy.toml`) and then
``modal deploy``s both the connector and the LiteLLM proxy against
the workspace named in the same `deploy.toml`. The
`--yes-i-mean-production` flag is the mandatory safety bar for tier
deploys; substitute `--yes-i-mean-staging` (and `activate staging`)
for the staging tier.

## Step 5: Bake one or more pool hosts

Pool hosts are baked as bare-metal slices. A slice bake carves a lima VM on a
`ready` bare-metal box, runs the default workspace template's `mngr create --template main
--template pool_host` to build + bake the agent state inside it, then writes a
`pool_hosts` row.

First order + set up the bare-metal box(es) the slices will be carved on (the
box must be `ready` and have a free slot). All of these are env-aware: with the
tier activated they resolve the OVH credentials, pool DSN, and pool SSH key
from Vault / the env's local state, so nothing is hand-exported:

```bash
just order-server --dry-run --plan-code ... --region ...  # price/spec preview, no charge
just order-server --plan-code ... --region ...            # order a box from the supplier
just await-delivery <bare-metal-server-id>                # wait for the serviceName + IP
just setup-server <bare-metal-server-id>                  # reinstall (injects our host key) + composed prep -> `ready`
uv run minds-admin server register ...                    # (alternative) record an already-provisioned box

# Inspect / (re-)prep:
just list-servers                                  # find the ready box's id
just prep-server <bare-metal-server-id>            # re-run just the prep step
```

Gen-2 orders and registrations are **units-validated** (specs/slice-fleet):
the box's disk must hold its RAM's full complement of default-size machines
(each machine = a 10GiB boot disk + a data disk of 16GiB + 3.5GiB per unit), or the
command refuses -- there is deliberately no storage add-on ordering; pick a
larger storage config instead. `minds-admin server pricing` marks each row's
base storage with a `UNITS_VALID` column, and `server list` shows gen-2 boxes'
capacity as used/total units and disk (gen-1 boxes keep the slot display).

Gen-2 capacity is **two-budget accounting**, not slot counting: a box's
machines are checked against its memory-unit budget (RAM minus the 8GiB host
reserve, each machine consuming its units plus a 512MiB per-VM overhead) and
its disk budget (the measured XFS storage partition minus a fixed 64 GiB
reserve: the 32 GiB swapfile, the 16 GiB image tar cache, the 4 GiB staged
base image and a 12 GiB staging margin -- all of which live on the storage
partition; the box row's `disk_gb` is the partition size the gen-2 prep
measures and records, not the catalog figure, which is why a gen-2 box must
be prepped before anything is carved on it), summed from the recorded
per-slice env files under the box's allocation lock. The pool itself stays
uniform (every bake carves the default 8-unit machine; `pool create --units`
exists for dev/testing); other sizes are reached by resize-then-restart, and
a restore may **evict** unleased `available` pool rows to fit a big machine --
pool depth after an eviction (the `pool_rows_evicted` metric) is the
operator's re-bake signal (replenishment stays manual).

A gen-2 bake creates the workspace container under gVisor (`runsc`, which the
gen-2 guest image ships) with `/run` and `/tmp` on tmpfs. `pool create
--docker-runtime runc` bakes a plain-runc slice next to a runsc one for a
side-by-side comparison (dev/testing only; refused for gen-1 boxes, whose lima
guest has no runsc).

`just prep-server <id>` (wrapping `minds-admin server prep`) re-runs just the
prep step: qemu/lima/tooling + image staging + the per-box DEFAULT_WORKSPACE_TEMPLATE image
cache dir, plus the observability collector when the tier has a boxes ingest
credential in Vault (installed and verified active, fail-closed; no credential
= clean skip). `just setup-server <id>` runs the same composed prep after its
destructive OS reinstall, so a `ready` box always matches the prep's desired
state. Both SSH the box with strict host-key pinning, so the box's sshd
host key must already be recorded on its `bare_metal_servers` row -- which
`server setup` does at OS reinstall, or `minds-admin pool backfill-host-keys` captures
once for a box installed out of band. `prep` fails closed (no trust-on-first-use)
if no host key is recorded.

Note: boxes prepped before 2026-06-27 lack the per-box DEFAULT_WORKSPACE_TEMPLATE image cache directory
that production (`--from-tag`) bakes require -- re-run `just prep-server <id>`
(idempotent) on such a box before baking on it.

Then bake slices onto a chosen box, after activating the tier:

```bash
eval "$(uv run minds-admin env activate production)"   # or `staging`
just bake-slice-prod US-WEST-OR v0.3.0 1 --server-id <bare-metal-server-id>
```

The `just bake-slice-{dev,prod}` recipes wrap `minds-admin pool create`
(`apps/minds_admin/imbue/minds_admin/cli/pool.py`), the env-aware command
that, from the activated tier:

- reaches the box with your operator SSH certificate (gen-2: signed on demand
  by the tier's Vault SSH CA into `~/.mindsadmin/<tier>/ssh_id`) or, on a
  gen-1 box, the tier's pool key from the
  `secrets/minds/<tier>/pool-ssh/POOL_SSH_PRIVATE_KEY` Vault leaf -- the same
  key the connector loads at lease time (you never generate or pass a key by hand);
- for staging / production, reads the host_pool DSN from
  `secrets/minds/<tier>/neon/DATABASE_URL` (those tiers keep no local
  secrets.toml); dev / ci envs auto-resolve it from their per-env secrets.toml.

The `region` argument is the lease-region **label** stamped on each row (what the
connector region-matches at lease time, e.g. `US-EAST-VA`) -- not the box's raw
datacenter code.

The `--attributes` JSON only *labels* the row for lease matching -- it does NOT
select the baked version. **The baked version comes entirely from the bake
source:** `--from-tag <tag>` (production; clones the DEFAULT_WORKSPACE_TEMPLATE remote at an exact tag)
or `--workspace-dir <dir>` (dev; a working tree, default `$DEFAULT_WORKSPACE_TEMPLATE_DIR` -- from your
shell or a gitignored `apps/minds/.env` -- else the
`.external_worktrees/default-workspace-template` checkout). The minds desktop client always sends
`repo_branch_or_tag` in its lease request (the resolved DEFAULT_WORKSPACE_TEMPLATE branch in dev, or the
latest semver tag in production), so that key must be present on every row that
should ever be leased. Other dimensions (`cpus`, `memory_gb`, `gpu_count`) can be
set for a more constrained pool generation; they're only required on the row when
the lease request also includes them. For slices, the per-slice size
(`memory_gb` / `cpus`) is computed from the box and stamped automatically.

`minds-admin pool create` runs the host-creation step in-process (the
bake machinery in `apps/minds_admin/imbue/minds_admin/bake/`). For one-off
baking of a gen-1 box outside an activated env, pass the `--database-url` /
`POOL_SSH_PRIVATE_KEY` overrides explicitly; a gen-2 box always needs an
activated env, because the operator certificate is signed by that tier's CA.

### Networking on a gen-2 box

Each slice VM sits alone on its own routed tap (`mslice<N>`) with a private
/30 derived from its ordinal: the box side is the gateway, the VM side the
address the per-VM nftables DNAT and anti-spoof rules expect. The VM learns
that address by DHCP: prep installs dnsmasq (`dnsmasq-base`, run by mngr's own
`mngr-slice-dhcp.service` on `/etc/mngr/slice-dhcp.conf`, DHCP only -- it
never serves DNS), bound dynamically to the taps, with one single-address
range per ordinal, the public resolvers pushed as option 6, and leases keyed
by the ordinal-derived MAC. The guest's cloud-init `network-config` is
therefore a placement-free `dhcp4: true`, and the whole cidata (user-data,
meta-data, network-config) is written once at carve and copied verbatim by
every restore, so cloud-init runs exactly once per VM: a restore onto another
ordinal or box changes nothing inside the guest but the address it is handed.
The DHCP config, unit and udp/67 policy are prep artifacts like the slice
unit, helper and sudoers (content-converged, in the telemetry integrity
manifest), and the slice units `Wants=` the DHCP unit so a rebooted box never
starts a VM before its DHCP server.

The server is treated as attacker-reachable (its DHCPv4 parser is fed by the
guests) and confined accordingly: the unit starts dnsmasq as the dedicated
unprivileged `mngr-dhcp` system user (never root) under a systemd sandbox
mirroring the slice unit's (`ProtectSystem=strict` with only its lease
directory writable, no devices, no namespaces, a syscall filter, IP traffic
limited to the slice /30s and the DHCP broadcast addresses;
`systemd-analyze security` scores it 1.4 OK), holding exactly one
capability, `CAP_NET_BIND_SERVICE`, to bind udp/67. It needs neither
`CAP_NET_ADMIN` nor `CAP_NET_RAW`: the config answers unconfigured guests by
broadcast (`dhcp-broadcast`, so no ARP-cache injection) and never ICMP-probes
an address (`no-ping`), so a compromised server cannot reach the
`mngr_slices` rules, the routes or the taps. The DHCP socket is
wildcard-bound, so a box-level nftables policy in its own table
(`inet mngr_slice_dhcp`, `/etc/nftables.d/mngr-slice-dhcp.nft`, loaded by
`nftables.service` at boot like the management lockdown's policy) drops
udp/67 arriving on any interface but the `mslice*` taps before the socket
sees it; udp/68 is untouched because the box's own uplink is a DHCP client
of the supplier. To debug addressing on a box: `journalctl -u
mngr-slice-dhcp`, the lease file under `/var/lib/mngr-slice-dhcp/`, and the
policy's drop counter (`nft list table inet mngr_slice_dhcp`).

### Disk layout on a gen-2 box

The gen-2 reinstall carves a 1 GiB `/boot` and a 20 GiB `/` (the OS, apt and
the prep artifacts only) and hands the rest of the mirrored disk to the
storage partition at `/srv/mngr-slices`. (OVH's installer adds its own
mirrored EFI system partition in front, which is why a gen-2 box shows four
md arrays for our three layout entries, plus a tiny unmirrored `config-2`
cloud-init partition.) The storage partition holds every slice's disks, the
staged base image, the box swapfile (`/srv/mngr-slices/swapfile`) and the
per-tag image tar cache (`/srv/mngr-slices/image-cache`). Gen-1 boxes keep
their `/swapfile` and their home-dir tar cache.

### Storage encryption on a gen-2 box

The gen-2 prep formats the storage partition as a **LUKS2 volume**
(`aes-xts-plain64`, 4 KiB sectors, discards allowed) and mounts the opened
mapper `/dev/mapper/mngr-storage` at `/srv/mngr-slices`, so everything on it
-- the slice disks, the base image, the tar cache, the swapfile -- is
ciphertext at rest. It covers a pulled or replaced NVMe (a RAID rebuild
copies ciphertext), OVH rescue mode without our key, and hardware
decommissioning. It does not cover us: we hold the recovery passphrase, the
box unlocks itself, and box root reads the mapper. Live slices are not
private from the operator (see the addendum in
[security-boundaries-audit.md](../security-boundaries-audit.md)).

Two keyslots open the volume:

- **The box's TPM 2.0** (`systemd-cryptenroll --tpm2-device=auto`, sealed with
  no PCR policy), which `systemd-cryptsetup` uses at boot through the
  crypttab entry (`tpm2-device=auto,headless=true,nofail`). No PCR policy means
  kernel and firmware updates never lock the box; the price is that any OS
  booted on that exact TPM -- rescue mode included -- can open it, which is
  inside the operator trust boundary anyway.
- **A per-box recovery passphrase**, minted by `minds-admin server prep` /
  `setup` and stored in the tier's Vault at
  `secrets/minds/<tier>/box-storage/<ovh-service-name>` **before** the
  partition is formatted (a prep that dies mid-format never leaves an
  unrecoverable box). The passphrase never rides inside the prep script: the
  CLI stages it on the box's tmpfs over its own SSH round trip and the prep
  consumes and deletes it. Every re-prep verifies the Vault passphrase still
  opens a keyslot and re-seals the volume to the box's current TPM (a cleared
  or replaced TPM leaves its stale token in the LUKS header, so the
  enrollment is redone rather than trusted).

A LUKS header backup is staged by every prep and uploaded by the CLI to the
tier's workspace-storage bucket at
`<env-prefix>boxes/<ovh-service-name>/luks-header-<luks-uuid>.img`
(`cryptsetup luksHeaderRestore` is the way back from a corrupt header; the
RAID mirror does not protect against a bad write). A tier without a usable
storage bucket logs a warning and skips the upload.

**Encryption is a precondition, not an option.** The prep formats only an
*empty* storage partition (the state OVH's reinstall leaves) and refuses one
that already holds slices: there is no in-place conversion, so a box prepped
before storage encryption existed is drained and repaved (`minds-admin
server drain`, then `cutover repave` or `server setup`). Every bake refuses a
gen-2 box whose storage root is not the mounted LUKS volume, and
`server list --verify-occupancy` / `just audit-boxes` report
`is_storage_encrypted` per box. The prep also converges three relocations so
nothing user-adjacent stays on the plain root partition: the box journal
(which carries the guest consoles), the slice service user's home (where
transfers stage S3 credentials and decrypted cidata), and `/tmp` + `/var/tmp`
are bind-mounted from `/srv/mngr-slices/system/` by prep-installed mount
units. The journal and the home are carried over and their root-side
directories left as empty stubs (the home root-owned, so a locked box has no
writable home to stage plaintext in); the temp directories are only covered by
their binds, mounted `nosuid,nodev` like the distro's tmpfs. Debian 13 mounts
`/tmp` as a RAM-backed tmpfs by default; the prep's `tmp.mount` replaces it,
so `/tmp` no longer competes with the slices for the box's memory.

**When the TPM unlock fails at boot** (a cleared or replaced TPM, a firmware
fault), the crypttab entry's `nofail` lets the box boot: sshd and WireGuard
come up, the storage root stays unmounted, the slice units stay down (their
`RequiresMountsFor` on the storage root is unmet), and the box telemetry
collector raises the `STORAGE_VOLUME_LOCKED` signal. Open it by hand:

```bash
uv run minds-admin server unlock --server-id <bare-metal-server-id>
```

which feeds the Vault passphrase to the box on stdin, opens and mounts the
volume, restores the bind mounts and the swapfile, flushes the journal, and
starts every slice unit that is enabled for boot. Re-run `just prep-server
<id>` afterwards to re-seal the volume to the TPM. A box whose passphrase is
lost from Vault cannot be recovered: drain it and repave.

### Disk layout inside a gen-2 slice

A gen-2 slice has two thin qcow2 disks. The **boot disk** (10 GiB) holds only the
guest OS and its journal (capped at 512 MiB): nothing a workspace does grows it.
The **data disk** (16 GiB + 3.5 GiB per unit; 44 GiB for the default 8-unit
machine) is whole-disk btrfs with *simple* quotas enabled and holds everything else:
docker's `data-root` (`/mnt/mngr-data/docker`: metadata, volumes, build cache),
containerd's root (`/mnt/mngr-data/containerd`: Docker's image store, so the
workspace image -- ~13 GiB on disk as overlayfs snapshots plus compressed
content blobs -- and every container's writable layer live there), the
workspace's home subvolume (`/mnt/mngr-data/<host_id>`), and the backup
snapshots. Everything the workspace writes -- its home, the image, and its
container layers in the snapshotter (so `apt install` inside the container
counts) -- shares **one** btrfs qgroup
(`1/0`) limited to the disk minus a 4 GiB system reserve; the grow oneshot
re-derives that limit from the filesystem on every boot, so a machine resize
grows the quota along with the disk (a fresh 8-unit machine has ~27 GiB free under
its 40 GiB limit). The engines' own metadata, docker's GC-bounded build cache, the newest backup
snapshot's delta and btrfs metadata live in the reserve, outside the quota, so
a workspace that fills its quota gets `ENOSPC` in its own files and container
rootfs while dockerd, sshd and the snapshot helper keep working. The backup
depends on that reserve: `host_backup` can still snapshot a workspace sitting
at its quota because the snapshot's metadata and copy-on-write delta land
outside the qgroup, and it deletes the snapshot as soon as restic has read it
because a retained one grows into the reserve (and pins the workspace's
deleted data) as the workspace churns. Gen-1 (lima) slices keep their 32 GiB
boot disk with docker on it.

### Fast path vs. slow path

When a user creates an imbue_cloud workspace, minds makes up to two `mngr create` calls:

1. **Fast path** (`fast_mode=require`): lease a pool host whose `attributes` exactly match (including `repo_branch_or_tag`) and adopt its pre-baked agent. This is fast because the host is fully baked.
2. **Slow path** (`fast_mode=prevent`): if no exact match exists, the provider raises `FastPathUnavailableError`; minds automatically retries, this time leasing *any* available host (resource attributes only -- `repo_branch_or_tag` is dropped), destroying its baked container, and rebuilding it from the DEFAULT_WORKSPACE_TEMPLATE `Dockerfile`. This is slower (a full container build) but works whenever the pool has any free host of the right size.

So a pool whose rows are baked at an older `repo_branch_or_tag` no longer hard-fails newer workspace creations -- they fall back to the slow path. Keeping the pool baked at the current version is still worthwhile because it keeps creations on the fast path. Only when the pool is genuinely empty (no `available` rows) does creation fail, with `ImbueCloudLeaseUnavailableError`.

To rsync the local mngr working tree into the DEFAULT_WORKSPACE_TEMPLATE worktree's `system/vendor/mngr/`
for the duration of the bake (dev-loop pattern; see
`apps/minds/docs/vendor-mngr-sync.md` for the sync mechanisms), forward
`--mngr-source <monorepo-root>` as an extra flag through the recipe. The bake
resets `system/vendor/mngr/` to HEAD when it finishes, so the worktree stays clean wrt
mngr churn.

List the rows (with the tier activated):

```bash
just list-pool-hosts
```

Audit the boxes themselves (real occupancy across every env, plus any
cross-tier contamination):

```bash
just audit-boxes
```

`just list-servers`'s slot columns come from the activated env's own
`pool_hosts` rows, so a box shared with another env reads as emptier than it
is. `audit-boxes` SSHes each box instead, and flags a box that a bake would
now refuse: one carrying another *tier's* slices, one whose service user
authorizes a static key it should not (exactly one on gen-1, none on gen-2),
or a gen-2 box whose pinned SSH CA is not the tier's.

## Step 6: Verify

```bash
psql "$NEON_DB_DIRECT" -c "SELECT id, vps_address, status, attributes FROM pool_hosts ORDER BY created_at DESC"
```

## Cleanup

Destroy pool hosts (destroys each slice lima VM, freeing the box slot, then
drops the row). Multiple ids are destroyed in parallel (bounded by
`--max-concurrency`, default 8), and each row is atomically claimed in the DB
(flipped to status `removing`) before its VM is touched, so a user lease
attempt can never race a destroy -- a row that got leased first is skipped and
reported:

```bash
just list-pool-hosts                             # find the row ids (tier activated)
just destroy-pool-hosts <id> [<id> ...]          # management credentials + DSN from the tier's Vault
```

Unleased (`available`) rows are destroyed without extra flags; a `leased` row
is refused unless you pass `--force` (which tears down the leasing user's live
workspace). A `baking` row (a bake in flight: `pool create` inserts the row
*before* it carves the VM, so the reap below can tell an in-flight slice from an
orphan) is only claimable once it is older than a bake could take (2 h); a live
bake's row is skipped and reported. A destroy that fails partway leaves the row in status `removing`
(unleasable); re-run the same command with the same ids to retry -- ids whose
rows are already gone report `already_gone` and count as success. Pass
`--drop-row-only` to drop rows without attempting VM teardown; that is only
for rows whose bare-metal box record is gone or whose machine is permanently
dead (the default path already tolerates a VM that is merely absent).

### Reaping orphans

A slice VM or data disk on a box with no `pool_hosts` row in this env -- a
`mngr create` killed after carving, a hand-carved slice, a disk a failed
rollback could not unlock -- holds a box slot forever. `pool create` reaps such
orphans after every bake, and the same reap runs on demand:

```bash
uv run minds-admin pool reap-orphans --server-id <id> --dry-run   # report only
uv run minds-admin pool reap-orphans --server-id <id>
```

Only slices stamped for the activated env are considered, and two guards keep a
carve that has no row yet safe: a VM that is running, or whose on-box state is
younger than 2 h, is spared (listed as `spared_instances`), and the data disk
of a running or spared VM is never deleted (qemu would keep running on the
unlinked inode and lose the disk at its next restart; a spared VM would be
destroyed by losing its disk just the same). Run it against a box whose bakes
you are not currently sharing with another operator.

### Upgrading the pool

To roll the pool to a new DEFAULT_WORKSPACE_TEMPLATE version, bake the new generation first, then
destroy the old `available` rows in one command:

```bash
just bake-slice-prod US-WEST-OR v0.4.0 4 --server-id <bare-metal-server-id>
just list-pool-hosts                             # note the old-version rows with status 'available'
just destroy-pool-hosts <old-id-1> <old-id-2> <old-id-3>
```

Hosts leased at the old version keep running until their leases end (the
connector destroys each slice VM at release). A user who leases an old row
mid-upgrade simply keeps it until release; the destroy skips it and reports
`skipped_leased`.

## Box maintenance

A box that needs a kernel reboot, a disk swap, a repave, or retirement is taken
out of service with `server drain`, which is generation-agnostic and stays
after the gen-2 cutover:

```bash
uv run minds-admin server drain --server-id <id>      # re-run until it reports no remaining rows
# ... reboot / repair / repave the box ...
uv run minds-admin server undrain --server-id <id>    # only for a box still on 'draining'
```

`drain` marks the row `draining` (excluded from bakes, restore candidates, and
restart-in-place), destroys the box's unleased `available` rows so the
connector's lease cannot hand them out, and force-stops every leased workspace
through the connector's admin stop. Owners see the ordinary
stopped-then-restoring flow; on their next start those workspaces restore onto
whichever box has room and do not return to this one. `undrain` only reverses
the status flip, so re-bake the box afterwards to give it pool rows again. A
repave (`server setup` / `cutover repave`) lands on `ready` by itself. Setting
`draining` through `server set-status` is refused, because the bare status flip
would leave the `available` rows leasable.

## Development workflow

During development, set `MINDS_WORKSPACE_BRANCH` to your branch name. The minds
app uses that branch as the lease request's `repo_branch_or_tag`, so the pool
host's `attributes.repo_branch_or_tag` must match. Bake against your dev env
(the DSN auto-resolves from its `secrets.toml`, and `--mngr-source` rsyncs your
live mngr tree into the DEFAULT_WORKSPACE_TEMPLATE worktree's `system/vendor/mngr/` for the bake):

```bash
eval "$(uv run minds-admin env activate dev-<your-user>)"
just bake-slice-dev \
    US-WEST-OR \
    "$PWD/.external_worktrees/default-workspace-template" \
    1 \
    --server-id <bare-metal-server-id> \
    --repo-branch-or-tag "$(git rev-parse --abbrev-ref HEAD)" \
    --mngr-source "$PWD"
```
