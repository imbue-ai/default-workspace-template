---
name: access-imbue-cloud-workspace
description: Reach a minds pool host, its slice VM, and the workspace container inside it from the laptop, read-only by default -- pool coordinates from the tier's Neon DB, the pool SSH key from Vault, `docker exec` into the container -- plus the few sanctioned operator mutations. Use when investigating one user's imbue-cloud workspace (backups, volume layout, logs, processes, disk), a pool host, or when a fleet survey needs per-host access.
---

# Accessing an imbue-cloud workspace

This applies to any tier (`production`, `staging`) and to personal dev envs; only the Vault paths and the activated env differ.
A minds imbue-cloud workspace is three nested things: a bare-metal **box** (OVH, in `bare_metal_servers`), a lima **slice VM** on that box (one `pool_hosts` row), and a docker **container** inside the VM where the user's agents run.
Everything you want to read lives in the container or on the VM's btrfs data disk; the box is only a hop.
There are two doors in, and which one you use depends on whose workspace it is.

| Door | Reaches | Needs | Works for |
|---|---|---|---|
| `mngr exec` through your own minds install | the container, via the connector | a signed-in minds app for the tier | workspaces leased to **your** account |
| pool SSH key to the VM root | the VM, then `docker exec` into the container | Vault role for the tier | **any** workspace on the tier |

Reads are the default.
Say "read-only" in your report when that is what you did, and name the owner of any workspace before you change anything on it (see Mutations).

## Prerequisites

Vault needs the HCP address and namespace exported before every `vault` command, the login included; without them the CLI talks to localhost and fails with "connection refused", which is a missing address, not a logged-out token (`minds-dev-workflow` has the details).
The login is browser OIDC and the token lasts about a week.
The role selects the tier grant: `role=minds_production` or `role=minds_staging` for a tier, and no role at all for a personal dev env, since the default `employee` role is what dev envs use and it is denied on the tier secret paths.

```bash
export VAULT_ADDR=https://vault-cluster-public-vault-df29b16f.9b573ab7.z1.hashicorp.cloud:8200 VAULT_NAMESPACE=admin
vault login -method=oidc role=minds_production   # role=minds_staging for staging; omit role= for a dev env
vault token lookup -format=json                  # ttl, and policies naming the tier you are about to read
```

A 403 on a `minds/<tier>/...` path right after a login usually means the wrong role for that tier; a 403 from a token that worked days ago means it expired.
Before a multi-hour run that reads Vault as it goes, check the ttl covers it.

`psql` on macOS comes from `brew install libpq`; put `/opt/homebrew/opt/libpq/bin` on `PATH`.

## Coordinates: the pool DB

The tier's Neon DSN is in Vault; `minds-admin` commands read it themselves once the env is activated, but for ad-hoc `psql` fetch it directly:

```bash
DB=$(vault kv get -mount=secrets -field=value minds/<tier>/neon/DATABASE_URL)
psql "$DB" -x -c "SELECT ... "         # -x for one wide row, -At -F $'\t' for machine-readable
```

The tables that matter:

| Table | Key columns | Notes |
|---|---|---|
| `pool_hosts` | `id` (the connector's host_db_id), `host_id`, `host_name`, `status`, `vps_address`, `ssh_port`, `container_ssh_port`, `lima_instance_name`, `bare_metal_server_id`, `leased_to_user`, `leased_at`, `agent_id`, `attributes->>'repo_branch_or_tag'`, `artifact_manifest`, `transition_*` | one row per pool host, slice or VPS; `agent_id` is the **baked** primary agent |
| `workspace_records` | `user_id`, `host_id`, `agent_id`, `display_name`, `state` | the client-side record synced up; `state='active'` rows are live workspaces |
| `bare_metal_servers` | `id`, `public_address`, `region`, `lima_service_user` | the box a slice lives on |
| `account_attribution` | `user_id`, `email` | `leased_to_user` is the first 16 hex of the user id with dashes removed; join on `substr(replace(user_id,'-',''),1,16)`. Sparse: it only holds users who signed up after attribution tracking began (45 of 119 leased users in September 2026), so the email is often null. Emails live in SuperTokens, not the pool DB; `minds-admin account show <email>` maps an email to its `user_id` |

`vps_address` is the **box's** address; lima forwards `ssh_port` to the VM's root sshd and `container_ssh_port` to the workspace container's sshd.
`status` values you will meet: `available`, `leased`, `stopping`, `stopped`, `starting`, `released`, `removing`.
A stopped row has no running VM; its disks are an uploaded artifact and only its layout at stop time is knowable.

A useful proxy: a leased host whose active `workspace_records.agent_id` differs from `pool_hosts.agent_id` was **rebuilt** at lease time (the slow path) rather than adopted from the bake.
It is a proxy, not a verdict; probe the VM before acting on it.

Find one workspace by name:

```sql
SELECT p.id, p.host_id, p.host_name, p.status, p.vps_address, p.ssh_port, p.container_ssh_port,
       p.attributes->>'repo_branch_or_tag' AS baked_tag, a.email
FROM pool_hosts p
LEFT JOIN account_attribution a ON substr(replace(a.user_id,'-',''),1,16) = p.leased_to_user
WHERE p.host_name = '<name>' ORDER BY p.leased_at DESC;
```

## Door 2: the pool key to the VM

```bash
umask 077
K=$(mktemp /tmp/poolkey.XXXXXX)
vault kv get -mount=secrets -field=value minds/<tier>/pool-ssh/POOL_SSH_PRIVATE_KEY > "$K"
printf '\n' >> "$K"          # -field=value strips the trailing newline some ssh builds require
KH=/tmp/pool_known_hosts     # private known_hosts: slices reuse box:port pairs, so never use ~/.ssh/known_hosts
ssh -i "$K" -o UserKnownHostsFile=$KH -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 -o BatchMode=yes \
    -p <ssh_port> root@<vps_address> "$(cat /tmp/remote.sh)"
rm -f "$K"
```

Write the remote command to a file and pass it with `"$(cat file)"`.
Nested quoting inside `ssh ... 'docker exec ... sh -c "..."'` is where these sessions go wrong, and a file can be syntax-checked with `bash -n` first.
Batch everything you want from one host into one round trip, and run `grep`/`wc`/`sed -n` remotely; the logs are megabytes.
Never `tail`/`head` a pipeline; write the result to a file and read that.

The pool-hosts runbook (`apps/minds/docs/deploy/ops/pool-hosts.md`, "Verify a baked slice") has a ready-made wrapper script for the container port; the pattern here is the same against the VM root port.

### On the VM

```bash
cid=$(docker ps -q --filter label=com.imbue.mngr.host-id | sed -n 1p)   # the workspace container
docker inspect -f '{{.Name}} {{.Created}} {{index .Config.Labels "com.imbue.mngr.provider"}}' "$cid"
vol=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/mngr-vol"}}{{.Source}}{{end}}{{end}}' "$cid")
ls -A "$vol"                          # home/ = current layout; only agents/ host_dir/ host_state.json = legacy layout
M=$(readlink -f /mngr-btrfs)          # the btrfs data mount; $vol is docker's bind of a subvolume under it, NOT a btrfs root
ls -d "$M"/snapshots/*/ | sort | sed -n '$p'   # the outer snapshots host_backup reads; do not create or delete under snapshots/
df -h /                               # the VM root disk: docker's image store and the container overlay live here
```

The container's provider label says who built it: `imbue_cloud_slice` is the bake; `imbue_cloud_<user-email-slug>` is a client-side rebuild.
`docker exec "$cid" sh -c '...'` runs under `sh`; use `bash -c` when you need bash.

### In the container

Two generations exist.
Post-declutter workspaces (template tags from `minds-v0.3.10`) keep everything under `/home/user`: the checkout at `/home/user/workspace`, mngr data at `/home/user/.mngr`.
Pre-declutter ones keep mngr data at `/mngr` and the checkout at `/mngr/code`.
Check `readlink -f /home/user` and `readlink -f /home/user/.mngr` first; on the current layout both resolve onto `/mngr-vol`.

| What | Where (post-declutter) |
|---|---|
| workspace env file | `/home/user/.mngr/env` |
| agent records | `/home/user/.mngr/agents/<agent-id>/data.json` (labels carry `display_name`, `original_minds_version`) |
| backup events | `/home/user/.mngr/agents/<system-services agent-id>/events/backup/events.jsonl` |
| supervisor program logs | `/var/log/supervisor/<program>-stderr.log` (`host-backup`, `system_interface`, ...) |
| restic secrets | `/home/user/workspace/data/.secrets/restic.env` (pre-declutter: `runtime/secrets/restic.env` under `/mngr/code`) |

The workspace's own mngr only sees its agents with the env file sourced; without it every agent reads `STOPPED`:

```bash
docker exec -w /home/user/workspace "$cid" bash -c '
set -a; . /home/user/.mngr/env; set +a
MNGR_ALLOW_UNKNOWN_CONFIG=1 timeout 120 uv run mngr list --format json --provider local'
```

Agent states are `RUNNING` (generating), `WAITING` (idle but the claude process is alive and holds its cwd), `DONE`, and `STOPPED`.
The tmux server is at `/tmp/tmux-0/default` and also needs the env file.

Backup events are one JSON object per line with a `type` such as `SNAPSHOT_CREATED`, `RESTIC_BACKUP_SUCCEEDED`, `RESTIC_BACKUP_FAILED`, `FORGET_COMPLETED`, `PRUNE_COMPLETED`, `TICK_SKIPPED_DUE_TO_MISSING_SECRETS`.
Count them and read the last few with `grep -c` and `tac ... | sed -n 1,3p`, and read the failure reason from the host-backup stderr log.

## Door 1: `mngr exec` from your own install

For a workspace your account leases, the laptop's minds install reaches the container through the connector with no Vault at all.
Activation only prints exports and shell state does not persist between tool calls, so inline the variables (production shown; a dev env uses `~/.minds-<env>/...` and `MINDS_ROOT_NAME=minds-<env>`):

```bash
MINDS_ROOT_NAME=minds MNGR_HOST_DIR=$HOME/.minds/mngr MNGR_PREFIX=minds- \
  MINDS_CLIENT_CONFIG_PATH=$PWD/apps/minds/imbue/minds/config/envs/production/client.toml \
  uv run mngr exec agent-<hex> -- 'tac /var/log/supervisor/host-backup-stderr.log > /tmp/r; sed -n 1,20p /tmp/r'
```

Target the **agent id**, never a name: names resolve to hosts, `system-services` exists on every workspace, and a name fans out to every matching host and starts stopped ones (`--start` defaults on; pass `--no-start` when a stopped host must stay stopped).
The command runs under `sh` as one quoted string after `--`.
Each call costs 10 to 15 seconds of discovery, so batch.

## Mutations

The sanctioned operator paths, each through `minds-admin` so the connector's own transition logic runs:

| Action | Command | Credentials |
|---|---|---|
| stop a running workspace | `minds-admin workspaces stop <pool_hosts.id>` | `MINDS_ADMIN_KEY` from Vault `minds/<tier>/supertokens/MINDS_ADMIN_KEY`; connector URL from `MINDS_CLIENT_CONFIG_PATH` |
| probe or repair the home layout | `minds-admin repair-home-layout --host-id <host_id> [--migrate\|--rollback]`, `--all-leased` to probe the pool | `MINDS_HOST_POOL_DSN` and `POOL_SSH_PRIVATE_KEY`, or an activated env |
| release, abandon | `minds-admin workspaces release`, `abandon` | as for stop |

`eval "$(uv run minds-admin env activate <tier>)"` exports `MINDS_ROOT_NAME`, `MNGR_HOST_DIR`, `MNGR_PREFIX`, and `MINDS_CLIENT_CONFIG_PATH`, after which `minds-admin` resolves the DSN, pool key, and admin key from Vault itself.
Because tool-call shells do not persist, export those four plus the secrets inline instead, or add a justfile recipe if the task will recur (`minds-justfile`).

There is no operator route that **starts** a stopped workspace; `POST /workspaces/{id}/start` resolves the caller as the owner.
The rollout page `apps/minds/docs/deploy/history/rollouts/slow-path-rebuild-legacy-home-layout.md` records a DB nudge that moves a row to `starting` for the connector's hourly watchdog to pick up.
Treat it as break-glass: it skips the owner and quota checks, so say whose workspace it is and why before using it, and prefer adding an admin start route if you need it more than once.

Anything else on a user's VM or container is a change to a colleague's or customer's machine: freeing disk, killing processes, `git revert` in their checkout, restarting services.
Name the owner, state the change and its rollback, get the go-ahead, and afterwards report the state you **observed**, not the state you intended (a restart you did not watch come back is not "recovered").

## Related

- `fleet-survey-and-sweep` -- the same access applied to every host at once, and how to mutate hosts one at a time safely.
- `apps/minds/docs/deploy/ops/pool-hosts.md` -- baking, verifying, and retiring pool hosts.
- `apps/minds/docs/deploy/history/rollouts/slow-path-rebuild-legacy-home-layout.md` -- a worked investigation and repair using every path above.
- `minds-justfile` -- promote a recurring operator command into a recipe.
- `investigate-bug` -- when the evidence is server-side (connector, LLM proxy) rather than on the workspace.
