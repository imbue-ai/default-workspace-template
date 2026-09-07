# Pool hosts

Pre-baked machines a workspace create can lease in ~45 seconds instead of
building one from scratch in ~5 minutes. A pool host is a **slice**: a lima/QEMU
VM carved on a bare-metal box, running the default workspace template's container,
parked stopped, with a row in the tier's `pool_hosts` table.

This doc owns the recurring lifecycle: **size, bake, verify, retire.** Standing a
tier's pool infrastructure up for the first time -- the database schema, the pool
keypair, the Vault entries, ordering and prepping boxes -- is in
[../setup/tier-bringup.md](../setup/tier-bringup.md).

## What is baked, and what is safe to redo?

```bash
eval "$(uv run minds-admin env activate <tier>)"
just pool-list | jq -r 'map({s:.status, t:(.attributes.repo_branch_or_tag // "NONE"), r:.region})
  | group_by([.s,.t,.r]) | map({s:.[0].s,t:.[0].t,r:.[0].r,n:length})
  | sort_by(.s,.t,.r) | .[] | "\(.n)\t\(.s)\t\(.t)\t\(.r)"'
```

Count by status, tag and region. Only `available` rows at **this release's tag**
can serve a fast-path create — a client asks for its own `FALLBACK_BRANCH`
exactly, so available rows at older tags do not help it.

| status | means | `pool-destroy` |
|---|---|---|
| `available` | baked, unleased, leasable | yes |
| `unreachable` | lease-time quarantine: a lease could not reach the box | yes |
| `removing` | a release or a destroy that did not finish | yes -- re-run the same destroy |
| `released` | lease ended | yes |
| `leased` | somebody's running workspace | only with `--force` |
| `stopped` | somebody's stopped workspace: VM halted, artifact in object storage, **slot freed**. Counts against their quota | refused |
| `stopping` / `starting` | a stop or start in flight | refused |
| `crashed` | a transition failed | refused |

The four `refused` statuses are refused outright: `--force` only adds `leased`.

A `stopped` row's slot is freed only when its retention window closes -- until
then it keeps its halted VM and a non-NULL `bare_metal_server_id`. Since
`server-audit` counts the disks physically on the box, it sees that slot either
way, which is why free capacity is read from the audit rather than from row
statuses.

The bake is **additive and safe to redo**: it creates new slices and never
modifies existing ones, so a repeated or partial bake costs slots and time. The
sharp edges are the destructive commands:

| command | |
|---|---|
| `just pool-bake` | safe — a failed slice rolls itself back and writes no row |
| `just pool-destroy <id>` | **irreversible**; refuses a `leased` row without `--force` |
| `minds-admin pool teardown-slices` | **irreversible and unbounded** — every unleased row in the tier |

## Why a pool at all: fast path vs slow path

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

## Choose a box

```bash
just server-list                                        # DC codes, status, DB-derived slots
just server-audit > /tmp/audit.json 2> /tmp/audit.log   # the real numbers
jq -r '.boxes[] | select(.slot_count - .box_used_slots >= 1)
       | "\(.server_id)  \(.public_address)  free=\(.slot_count - .box_used_slots)"' /tmp/audit.json
```

Take the **full UUID** from that JSON — both tables truncate the id for display,
and `--server-id` refuses anything that is not an exact match.

`server-audit` prints its table to **stderr** and its JSON report to **stdout** —
redirect both or you see only the table.

Size from the audit, never from `server-list`: its `SLOTS` counts only this env's
`pool_hosts` rows, so a box shared with another env — or holding a VM whose row
was deleted — reads emptier than it is, and the bake refuses mid-run with
`MNGR_SLICE_BOX_FULL`. `server-audit` SSHes each box and counts the lima **data
disks** present across every env; a disk outlives its VM, so a carve that died
before registering still holds its slot. It also flags a box a bake would now
refuse: one carrying another tier's slices, or one whose lima user authorizes
more than the single pool key `prep` writes.

Per box, require:

| field | required | meaning |
|---|---|---|
| `status` | `ready` | anything else and the bake refuses |
| `slot_count - box_used_slots` | ≥ your count | genuinely free capacity |
| `authorized_key_count` | exactly `1` | prep writes one key; a second means someone added one by hand, and that holder has `limactl`, hence root, over every workspace on the box |
| `foreign_tier_slices` | `[]` | a box carrying two tiers' slices is a box both tiers' pool keys can SSH |
| `is_foreign_tier_checked` | `true` | `false` means an empty `foreign_tier_slices` is NOT CHECKED, not clean |
| `degraded_md_arrays` | `[]` | not a blocker, but a degraded array is one disk failure from losing every workspace on the box |

**A "slot" is empty capacity, not an idle workspace.** `slot_count` is what the
box's RAM can hold — `(RAM − 8 GB reserve) ÷ (slice GB + 0.5 GB overhead)`, so 14
on a 128 GB box. Free slots are room for VMs that do not exist yet. How many
*unleased baked rows* exist is a different question — see **Size a production
generation** below.

**Pair the region label to the box by hand.** `server-list` prints the OVH
**datacenter code**; the bake takes the **lease-region label**, and nothing
cross-checks them:

```
US-EAST-VA  ↔  vin  (Vint Hill, VA)
US-WEST-OR  ↔  hil  (Hillsboro, OR)
```

`pool create` rejects `vin`/`hil` outright as a `--region`, but it will happily
accept `US-EAST-VA` for a `hil` box and bake rows that advertise the wrong coast
and are therefore never leased.

## Bake a generation

> **`pool-bake` is the right recipe on every tier, staging included.** The
> `-prod`/`-dev` suffix names the **bake source**, not the tier. `pool-bake`
> is `--from-tag`: it clones the dwt remote at an exact tag, so the content
> provably equals the tag. `pool-bake-from-worktree` is `--workspace-dir`, which bakes a
> working tree including uncommitted edits *and* hard-codes
> `--skip-deferred-install-wait`, whose own help says never to use it for pool
> hosts. **The tier comes from the activated env** — its Neon DSN, its Vault pool
> key, and the env name stamped into each slice VM's name. Confirm it in the
> dry-run's `env_name`.

The dwt `minds-v<version>` tag must already be pushed: `--from-tag` runs
`git ls-remote --tags` and refuses before it reads Vault or clones. The **mngr**
tag is not needed — a `--from-tag` bake keeps the tag's own vendored mngr and
syncs no local mngr in, which is what makes slice content provably equal the tag.

```bash
just pool-bake <LEASE_REGION> "$VERSION" <count> --server-id <BOX_ID> --dry-run
```

`--dry-run` is a full rehearsal, not a stub: by the time it prints JSON it has
verified the tag on the remote, cloned it, read the pool key from Vault, SSHed
the box, listed its lima disks, and run the tier-exclusivity assertion. Check
`env_name`, `free_slots >= count`, and that `attributes.repo_branch_or_tag`
equals `$VERSION` — that is the exact string a client of this release sends at
lease time, derived from `--from-tag` and never typed.

```bash
just pool-bake <LEASE_REGION> "$VERSION" <count> --server-id <BOX_ID> 2>&1 | tee /tmp/bake.log
echo "bake exit: ${PIPESTATUS[0]}"        # zsh: ${pipestatus[1]}
```

`tee` swallows the real exit code, so read it from the pipe-status array — and
note the shells differ: bash `${PIPESTATUS[0]}`, zsh `${pipestatus[1]}`. The
authoritative answer is the report's `failed` field either way.

**What happens.** On a box with no tar for this tag the bake is **seed-then-fill**:
one slice is baked alone, builds the image inside its VM, and the box pulls it
out with `docker save` into `~/.cache/mngr-slice-default-workspace-template/`;
the rest then `docker load` it. The seed costs the most; fills are cheaper.

Both scale with how loaded the box already is, and the spread is wide enough that
a fill on a full box can cost what a seed costs on an empty one. Budget from the
box you picked, not from a previous release. The 25m per-slice deadline (lima's
own default is 10m) is sized for the loaded case.

**Reading the outcome.** Success is a `SliceBakeReport` with `requested ==
succeeded` and `failed: 0`. Each slice gets 3 attempts and a failed attempt
destroys its VM and writes no row, so retry lines in the log are normal. A seed
failure aborts the whole invocation before the fill runs, so `requested: N,
succeeded: 0` with one distinct error is **one** broken image build, not N
problems. `failed to listen tcp … address already in use` from the lima hostagent
is documented noise.

Every failure path rolls back on its own and leaves no `pool_hosts` row: the
provider undoes a failed `mngr create`, anything later deletes the VM *and* its
data disk, and a `finally` reaps orphans against the DB. That reap is scoped to
the activated env's stamped names and is **skipped entirely with no env
activated**, which is how an un-activated bake leaks a box slot.

## Verify a baked slice

`succeeded` means the row was written. It does **not** mean the container is
healthy: sshd hardening, the git-identity clear, and the deferred-install wait
are all best-effort in the bake — logged, never raised.

```bash
umask 077
# Substitute the tier you are verifying (staging, production, ...).
# This is the ONLY place in the runbook where a tier is typed by hand.
vault kv get -mount=secrets -field=value minds/<tier>/pool-ssh/POOL_SSH_PRIVATE_KEY > /tmp/pool-<tier>.key
printf '\n' >> /tmp/pool-<tier>.key   # -field=value strips the trailing newline; ssh rejects it without this
chmod 600 /tmp/pool-<tier>.key

cat > /tmp/verify-slices.sh <<'SCRIPT'
#!/usr/bin/env bash
# Usage: verify-slices.sh <box-address> <container-ssh-port>...
set -uo pipefail
KEY=${POOL_KEY:?set POOL_KEY to the tier key you just wrote}
BOX=$1; shift
KH=$(mktemp)                 # throwaway: slices reuse a box:port, so a shared
trap 'rm -f "$KH"' EXIT      # known_hosts carries an earlier slice's host key
run() { ssh -i "$KEY" -p "$1" -o UserKnownHostsFile="$KH" \
          -o StrictHostKeyChecking=accept-new -o ConnectTimeout=25 \
          -o BatchMode=yes root@"$BOX" "$2" || echo "SSH_FAILED(rc=$?)"; }
for P in "$@"; do
  echo "########## container port $P"
  run "$P" 'test -x /opt/fortress/tilion-fortress/tilion && echo DEFERRED_INSTALL_OK || echo DEFERRED_INSTALL_INCOMPLETE'
  run "$P" 'cd /home/user/workspace && uv run mngr list 2>/dev/null | tail -1'
  run "$P" 'git -C /home/user/workspace config --local --get user.name'
  run "$P" 'grep -rl provider-chooser /home/user/workspace/system/apps/system_interface/imbue/system_interface/static/ >/dev/null 2>&1 && echo CONTENT_OK || echo CONTENT_STALE'
  echo
done
SCRIPT
chmod +x /tmp/verify-slices.sh
POOL_KEY=/tmp/pool-<tier>.key /tmp/verify-slices.sh <BOX_ADDRESS> <container_ssh_port> [<port>...]
```

Use the script rather than inline `ssh … "…"` loops: the nested quoting and line
continuations are easy for a terminal to mangle into an interactive session. The
ports are `container_ssh_port` from the bake report — **not** `vm_ssh_port`,
which reaches the outer VM instead of the workspace container.

| check | want | why |
|---|---|---|
| `DEFERRED_INSTALL_OK` | ok | the bake only warns on timeout, so a slice can land `available` with Chromium half-installed |
| `mngr list` | `system-services` **STOPPED** | load-bearing: the lease *starts* the adopted agent, and that start is what runs the workspace bootstrap for its new owner |
| `git config user.name` | a neutral value such as `minds-bootstrap`, **not the operator's name** | finalize unsets the operator identity; the dwt bootstrap then supplies its own neutral fallback on every boot. A *present* identity is correct — the failure is seeing your own name |
| `CONTENT_OK` | ok | the real content proof |

`CONTENT_OK` greps the **built** frontend bundle for a string only this release's
content has (`provider-chooser` from 0.5.0 onward; pick a fresh marker for later
releases). Grep the bundle, not the source, and do not bother comparing
`git rev-parse HEAD` to the tag — `/home/user/workspace` is a **fresh repo** the
seed creates, with its own `Initial workspace commit`, so its SHA differs per
slice and never equals the tag. The bundle grep matters because the per-box image
cache is keyed by tag **name**, not content: if a tag is ever moved, a box that
already holds that tar silently loads the stale image while reporting N/N
succeeded. This is why tags are immutable once anything has run against them.

**These checks prove the container. They do not prove the row is leasable** —
that takes an actual lease, which happens by driving the desktop app:
[app-release.md](./app-release.md), *Verifying a release in a running workspace*.
Its fast-path check is the bake's real acceptance test, and the two halves are
sequenced by the `release-minds` skill.

`rm -P /tmp/pool-<tier>.key` when done -- and use a per-tier filename as above, so a staging key can never linger at the path a production run reuses.

A blank result or `SSH_FAILED` on **every** check is not a failing slice: it is
the wrong tier's pool key, or a box you cannot reach. `SSH_FAILED` on *some*
ports, with the log showing the key was accepted, is a stale host key — which the
throwaway `known_hosts` above exists to prevent. Either way, re-read the Vault
path before concluding anything about the bake.

## Production

Same sequence as above, against production, with three additions: sizing from
the live fleet, retiring the old generation, and the connector deploy.

**Bake before you deploy.** A deploy freezes `MINDS_WEB_TEMPLATE_REF` from
`FALLBACK_BRANCH` in the tree it ships, and browser creates (`/hosts/claim`)
match that tag exactly with **no rebuild fallback** — unlike the desktop, which
falls back to a slow rebuild. Deploying ahead of the bake breaks browser creates
until the bake lands.

Assume a fresh shell — a release usually pauses before this point:

```bash
cd "$MNGR"                                    # the mngr checkout; every recipe runs from its root
export VERSION=minds-v<version>               # the tag to bake
export VAULT_ADDR=https://vault-cluster-public-vault-df29b16f.9b573ab7.z1.hashicorp.cloud:8200 VAULT_NAMESPACE=admin
vault login -method=oidc role=minds_production
vault kv get -mount=secrets -field=value minds/production/pool-ssh/POOL_SSH_PRIVATE_KEY >/dev/null && echo "pool key readable"
ls .minds-deploy-recover-target-*.json 2>/dev/null && echo "resolve this before continuing"
eval "$(uv run minds-admin env activate production)"
```

Prove the Vault scope now -- a token on the wrong role fails at the
bake, several minutes later, with an error about the pool key rather than about
your login.

A leftover recover-target file from an interrupted deploy — staging's included —
blocks every `minds-admin env` command until resolved.

## Size a production generation

Two different numbers: how many **unleased rows** exist per version, and how many
slots are genuinely free. **Choose a box**'s free slots are empty capacity, not
idle workspaces.

```bash
just pool-list > /tmp/pool-production.json
jq -r 'map({s:.status, t:(.attributes.repo_branch_or_tag // "NONE"), r:(.region // "NONE")})
       | group_by([.s,.t,.r]) | map({s:.[0].s,t:.[0].t,r:.[0].r,n:length})
       | sort_by(.s,.t,.r) | .[] | "\(.n)\t\(.s)\t\(.t)\t\(.r)"' /tmp/pool-production.json
just server-audit > /tmp/audit-production.json 2> /tmp/audit-production.log
```

[order-boxes.md](../setup/order-boxes.md) owns production capacity policy. Its
standing default is one new box per region per release; filling existing free
slots instead is equally fine. Decide from the live audit, not from either
document's default, and record which you chose in the history entry.

Aim to leave each region roughly the unleased headroom it has at the outgoing
tag. **One invocation targets one box** — per-slice sizing is homogeneous per box
— and it refuses when `count` exceeds that box's genuinely free slots, so a
fleet-scale bake is several invocations. Concurrent bakes on **distinct** boxes
are safe: run the regions in parallel shells.

Bake and verify exactly as above, then lease one from the desktop and confirm it
served your bake ([app-release.md](./app-release.md), *Verifying a release in a
running workspace*). Only then retire the old generation:

```bash
# One old generation's available, never-leased rows: the only set safe to destroy.
jq -r '.[] | select(.status=="available")
           | select(.attributes.repo_branch_or_tag=="minds-v<old-version>")
           | select(.leased_to_user==null and .leased_at==null and .released_at==null)
           | .id' /tmp/pool-production.json
just pool-destroy <id> [<id> ...]
```

**`available` *and* never-leased is what makes a row safe.** Releasing a lease
sets `status='removing'`, never back to `available`, so a row that is `available`
with a lease timestamp is an anomaly to understand before destroying it, not a
row to sweep up. `leased` and `stopped` are somebody's workspace; `pool-destroy`
refuses `leased` without `--force`, and `--force` has no place in retiring a
generation.

**Never use `pool teardown-slices` here.** It takes no tag, box or owner filter
and destroys every unleased slice row in the whole tier's pool DB -- on
production that is the entire fleet's spare capacity, at every version.

## Retire a rehearsal's slices

On staging, destroy the rows *your* rehearsal created, by id:

```bash
just pool-list
just pool-destroy <id> [<id> ...]
```

The `pool teardown-slices` warning above applies here too. Keeping a spare slice
at the new tag is reasonable — it makes the next staging test instant.

**Check your remaining context before continuing.** Production rollout is long;
if the session is nearly full, stop and hand off with the tag pair, the build id,
what you verified, and what remains.

