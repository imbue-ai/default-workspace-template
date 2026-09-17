---
name: fleet-survey-and-sweep
description: Answer a fleet-wide question about minds pool hosts or workspaces by probing every host read-only in parallel and classifying from ground truth, then, if a remediation follows, apply it host by host with an activity gate, per-host verification of the real outcome, halt on first failure, and a driver that outlives the agent session. Use for "which hosts are in state X", "how many users are affected", and for rolling a repair across the affected set.
---

# Surveying a fleet, then sweeping it

Two phases, deliberately separate.
The **survey** touches nothing and can run against every host at once; its output is a classified list you can share.
The **sweep** changes hosts and runs one at a time, and only over a list the survey produced.
Per-host access (Vault, the pool DB, the pool key, `docker exec`) is in `access-imbue-cloud-workspace`; this skill is the scaffolding around it.

## Phase 1: survey

### 1. Candidates from the DB, ground truth from the host

A DB query gives you the candidate set and its coordinates in one round trip; it does not tell you what a host actually looks like.
Where a DB signal is a proxy (an `agent_id` mismatch, a bake tag, a record date), say so, use it to choose what to probe, and measure how often it was wrong once you have the probe results.
A stopped row has no VM to probe: give it its own "unverified" class rather than guessing.

Export the worklist as one tab-separated row per host, sorted so the order is stable across runs:

```bash
psql "$DB" -At -F $'\t' > /tmp/hosts.tsv <<'EOF'
SELECT p.host_id, p.host_name, p.status, p.vps_address, p.ssh_port, p.leased_to_user, p.leased_at::date
FROM pool_hosts p WHERE p.status = 'leased' ORDER BY p.leased_at;
EOF
```

### 2. One probe script, one output file per host

Put the remote probe in a file that prints `key=value` lines, and drive it by **line number** so the wrapper never has to quote a whole row:

```bash
cat > /tmp/probe_remote.sh <<'REMOTE'
cid=$(docker ps -q --filter label=com.imbue.mngr.host-id | sed -n 1p)
echo "container=$(docker inspect -f '{{.Name}}|{{.Created}}|{{index .Config.Labels "com.imbue.mngr.provider"}}' "$cid" 2>/dev/null)"
vol=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/mngr-vol"}}{{.Source}}{{end}}{{end}}' "$cid" 2>/dev/null)
echo "entries=$(ls "$vol" 2>/dev/null | tr '\n' ',')"
REMOTE

cat > /tmp/probe_one.sh <<'ONE'
#!/bin/zsh
n="$1"; line=$(sed -n "${n}p" /tmp/hosts.tsv)
IFS=$'\t' read -r host_id name hstatus addr port user leased <<< "$line"
out=/tmp/probe/$host_id.txt
ssh -i /tmp/poolkey -o UserKnownHostsFile=/tmp/pool_known_hosts -o StrictHostKeyChecking=accept-new \
    -o ConnectTimeout=15 -o BatchMode=yes -p "$port" root@"$addr" "$(cat /tmp/probe_remote.sh)" > "$out" 2>&1
echo "ssh_exit=$?" >> "$out"
ONE
chmod +x /tmp/probe_one.sh; mkdir -p /tmp/probe
seq 1 "$(wc -l < /tmp/hosts.tsv)" | xargs -P 12 -I{} /tmp/probe_one.sh {}
grep -L "ssh_exit=0" /tmp/probe/*.txt      # the unreachable ones
```

Twelve in parallel covers a few hundred slices in under a minute.
Record `ssh_exit` in every file so "unreachable" is a class in the report, not a gap.
`xargs -I{}` with the whole row as the argument overflows the command line; pass the line number.

### 3. Join, classify, keep the CSV

Join the probe files back onto the DB rows in a short Python step, add the email from `account_attribution` where it has one (it covers well under half of leased users; report the rest by `leased_to_user`), and classify every host into one of a fixed set of classes, including `unverified (stopped)` and `unverified (ssh failed)`.
Write the per-host CSV somewhere you will find again and report counts per class.
Numbers in the report come from counting the artifacts, not from memory; a tally that drifts from the files is the most common error in these writeups.

If the probe will be run again, promote it into a `minds-admin` command with a probe-only default and a fleet flag, as `repair-home-layout --all-leased` does; an ad-hoc script in `/tmp` is gone with the session.

## Phase 2: sweep

### Before the first mutation

- **Rehearse where failure is cheap.** A disposable workspace the owner has named, or a local docker or lima slice, before a real user's. The first attempt of a new procedure usually fails on something the code already told you.
- **Derive the preflights from the code that built the thing.** If the realizer mounts a btrfs subvolume through docker's volume path, check `findmnt -n -o FSTYPE` before snapshotting; if the home tree is in an overlayfs upper layer, expect `rename` to fail with EXDEV on image-layer entries and never let `mv` degrade into a copy onto the root disk.
- **Verify the quiesce with a process count.** Listing agents through the workspace's mngr requires its env file sourced, and an idle chat still holds a claude process with its cwd in the tree; after stopping, count processes whose `/proc/<pid>/cwd` is still under the path you are about to move.
- **Every exit path after the quiesce restarts what it stopped**, including the failure branches, and the failure detail names where the data is now.
- **Write the rollback artifact before the change** (a read-only btrfs snapshot, an aside copy) and print its path in the outcome.
- **Name the owner of each host** in the log line before touching it. A sweep that started with one colleague's disposable hosts will reach strangers' machines; that is the moment to notice.

### The driver

One host at a time, gated, verified, halting on the first failure:

```bash
#!/bin/zsh
set -u
LOG=/tmp/fleet_migration.log; mkdir -p /tmp/fleet
log() { echo "$(date -u +%H:%M:%S) $*" | tee -a $LOG; }
while IFS=$'\t' read -r -u 3 host_id name addr port user leased; do
    log "==== $name ($host_id) user=$user"
    act=$(vm "$addr" "$port" "$ACTIVITY")          # e.g. running=<n> waiting=<n> from the workspace's mngr
    case "$act" in running=0*) ;; *) log "DEFERRED $name: ${act:-could not read activity}"; continue ;; esac
    uv run minds-admin <repair> --host-id "$host_id" <mutating flag, e.g. --migrate> > /tmp/fleet/$name-$host_id.json 2> /tmp/fleet/$name-$host_id.err
    mstatus=$(python3 -c "import json;print(json.load(open('/tmp/fleet/$name-$host_id.json'))['outcomes'][0]['status'])" 2>/dev/null || echo noparse)
    log "migrate: $mstatus"
    case "$mstatus" in migrated) ;; *) log "STOPPING THE RUN: $name did not migrate"; break ;; esac
    # verify the real outcome, bounded: e.g. a new RESTIC_BACKUP_SUCCEEDED newer than the restart
    ...
done 3< /tmp/worklist.tsv
log "==== driver finished"
```

The activity gate reads the signal that matters (a chat mid-generation) and **defers** rather than waits; a deferred host goes on a retry list and is reported as still unfixed.
Verification checks the outcome the mutation was for, with a bounded wait; "the command exited 0" is not the outcome.
Per-host JSON and stderr files are the record; the log is the timeline.

Traps met writing exactly this driver:

- `zsh` reserves `status`; assigning to it aborts the script mid-loop with `read-only variable: status` on stderr, which a log that only captures stdout never shows. Use another name.
- A `while read` loop over a file must read on a separate descriptor (`read -u 3 ... done 3< file`), because `ssh` inside the loop consumes stdin and the loop ends after one host.
- The remote script dies of **SIGPIPE** at its next `echo` if the operator's ssh session drops, not SIGHUP (stdio is piped, not a tty); a remote script that must run to completion traps both.
- Check `bash -n` / `zsh -n` on every script you generate, including scripts embedded as strings, before the first host.

### Keep the driver alive across your own session

Agent sessions get suspended and torn down, and processes started from a tool call die with them; a `Monitor` on the log dies too.
Launch the driver in its own session and log to a file:

```bash
python3 - <<'EOF'
import subprocess
p = subprocess.Popen(["/tmp/fleet_driver.sh"], stdout=open("/tmp/fleet_driver.out", "w"),
                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
print("driver pid", p.pid)
EOF
```

macOS has no `setsid`, and `nohup ... &` under the harness is not enough.
On resuming, re-ground from the log and the per-host files (which hosts completed, which deferred, what the last line says) and rebuild the worklist minus completed hosts; never continue from what you remember.
A driver that reads Vault per host will start failing when the token lapses mid-run; check the ttl first and read secrets once into files the driver uses.

### Reporting

Report per class, from the artifacts:

| Group | Count | State |
|---|---|---|
| migrated and verified | | done |
| deferred (owner active) | | still unfixed; retry when idle |
| failed (rolled back) | | still unfixed; the failure detail |
| out of scope (different generation) | | left alone, and why |
| unverified (stopped, unreachable) | | how they will be reached |

Say what you did not do and what is still owed: the retry list, the cleanup pass for rollback artifacts, hosts left in a different state than their owner set.
Record the run, the counts, and the leftovers on the relevant rollout page under `apps/minds/docs/deploy/history/rollouts/`.

## Related

- `access-imbue-cloud-workspace` -- per-host access and the sanctioned mutations.
- `apps/minds/docs/deploy/history/rollouts/slow-path-rebuild-legacy-home-layout.md` -- a full survey and sweep, including what went wrong on the way.
- `apps/minds/docs/deploy/ops/pool-hosts.md` -- the pool itself.
