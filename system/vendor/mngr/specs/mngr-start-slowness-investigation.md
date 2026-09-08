# Why `mngr start` on imbue_cloud hosts is slow or hangs

Investigation notes, 2026-08-20. Measured live against Gabriel's production imbue_cloud
workspace `workspace-1` (`host-d35b1c7a2c6846ffbf6b297f09864994`, provider
`imbue_cloud_gabriel-imbue-com`, box 147.135.97.96), with his authorization, plus a fresh
re-fetch of Kanjun's bug-report logs (Sentry event `64193d01ccbd47f19d507d24e243d842`).

Method: `MNGR_HOST_DIR=~/.minds/mngr MNGR_PREFIX=minds- uv run mngr start
agent-16c3a7ed2a7b44c2a0c5989d05fe4e31 -vv`, stderr timestamped per line, three clean
steady-state runs plus one marker-invalidated run and one deliberately lock-contended run.
Remote-side state inspected over the container and VM sshds directly.

## Measured: where a healthy start's time goes

Steady state (host running, adoption marker current, no contention) is **13.2-13.8s inside
the command, 16.1-16.7s wall**. Three runs: 13.23s, 13.84s (includes the one-time
migration re-verify), 13.51s. Breakdown of run 2 (13.23s command span):

| Window | Cost | What happens |
|---|---|---|
| 0 -> 2.9s | 2.9s | Python interpreter + imports (all provider plugins, incl. azure/gcp/aws SDKs) before any work |
| 2.9 -> 4.1s | 1.2s | Discovery: provider listing; 3 dead hosts fail fast (no local key material -> no network attempt) |
| 4.1 -> 5.1s | 1.0s | Discovery visits an unrelated healthy host (geebspace's box: fresh SSH conn + `docker ps`) |
| 5.1 -> 7.0s | 1.9s | workspace-1: two separate fresh outer-SSH connections (discovery probe, then `get_host`'s `docker ps` + `docker inspect`), then the container connection |
| 7.0s | 0.04-0.12s | Cooperative host lock acquired (uncontended) |
| 7.0 -> 9.9s | 2.9s | Per-agent state-dir checks + agent data loads over SSH, sequential, ~0.5s per agent x 4 |
| 9.9 -> 11.3s | 1.4s | `tmux has-session` check; start is a no-op (agent already running). An actual SI launch adds ~5s |
| 11.3 -> 13.3s | 2.0s | `emit_discovery_events_for_host`: full re-list of all agents |
| 13.3 -> 15.7s | 2.4s | Resume-message step: `get_agents()` re-loads all 4 agents a third time; no resume message configured, so nothing is sent |

Totals per start: ~21 pyinfra exec round trips + 4-7 fresh SSH connection establishments;
agent data is listed/loaded three separate times. Every step is sequential. The cost scales
with RTT, with fleet size (discovery visits every host of the provider), and with agent
count on the host.

The adoption migration (marker schema v1 -> v2, what every host paid once after the 0.4.x
upgrade) measured **~0.9-1.2s**: `read_reconciler_state` + two served-key probes, ~3
paramiko connections, no healing needed. Run 3 re-stamped the marker to v2, and run 5
confirmed the second start does not pay it again.

## Findings from the handoff: verdicts

**Finding 1 (the hang is old; only the wait got longer) - CONFIRMED.** Fresh S3 re-fetch of
`minds.log`: 95 x `timed out after 120s` spread over FOUR different agents/hosts
(2026-08-06 -> 08-17), 11 x `timed out after 1260s` (08-18 -> 08-20), flipping exactly at
her 0.3.17 upgrade. The last four 1260s timeouts (Aug 20 10:23Z, 11:40Z, 12:59Z, 14:21Z,
all UTC) are unattended `Start-only restart ... skipping the stop step` dispatches at a
~77-82 min cadence (1260s burn + ~1h retry spacing).

**Finding 2 (adoption re-runs full SSH work every start) - PARTLY REFUTED.** The
`start_host` marker-discard is real (`instance.py:2349-2350`) but `start_host` only runs
when the container is actually down. On the common path (host reads RUNNING,
`was_host_started == false`), `ensure_adopted` with a current marker and no pending key
work is a pure-local no-op - verified live. The full verify, when it does run, is ~1s / ~3
connections (healthy case), not the estimated 10-25. It grows only when healing or key
rotation is actually needed.

**Finding 3 (unbounded lock wait) - CONFIRMED, mechanism refined.**
`start_agents_locked` blocks indefinitely by design (`flock 9`, no timeout;
`host.py:_build_remote_lock_command`). Demonstrated live: a held flock blocks the start
with a single INFO line ("Waiting to acquire host lock...") that `--quiet` (production)
suppresses entirely; the local side sits in `channel.recv()` with no channel timeout.
The ghost-holder hypothesis needed refinement:

- A locally-killed mngr with a LIVE network does NOT leave a ghost. Process death closes
  the TCP socket, sshd delivers stdin EOF, the `while read` holder exits. An orphaned
  *waiter* (observed live in `/proc` after killing a contended start) survives blocked in
  `flock 9`, but self-clears the moment it acquires: its first stdout write hits the dead
  session and it exits. It does bump the generation counter; harmless.
- A holder behind a SILENTLY dead path (laptop sleep, network switch, NAT state loss) is a
  real ghost, and it persists ~2h11m: the container sshd runs with
  **`ClientAliveInterval 0`** (the VM sshd has 120!), so only kernel TCP keepalive reaps it
  (`tcp_keepalive_time 7200` + 9x75s probes). Confirmed live on workspace-1's sshds.
- Legitimate long holders also exist: `mngr create` holds the same lock with
  `timeout_seconds=None` for its entire provisioning, and in-container boot hooks hold it
  locally. A wedged create (see Finding "no client-side liveness" below) holds it forever.

**Finding 4 (diagnosability gap) - CONFIRMED, with a twist.** `_run_mngr_capturing`
discards the killed subprocess's captured stdout/stderr (`workspace_recovery.py:347-348`)
and `--quiet` silences the console. BUT mngr file-logs at DEBUG by default into
`<host_dir>/events/logs/mngr/events.jsonl`, and the bug-report bundle DOES upload that file
(the previous agent believed no trace was recorded anywhere; on current main a
recovery-style start writes a full DEBUG timeline there - verified for both the CLI and the
production app's own start on Gabriel's machine). The twist: **Kanjun's uploaded copy of
that exact file contains zero rows from any mngr start** - only `latchkey.forward` rows -
across a 10h window containing four hanging starts. Her starts either hung before the
first log write or logged somewhere we cannot see. Unexplained; the subprocess
output-capture fix is what will settle it.

**Finding 5 (0.4.x migration broke hosts) - MECHANISM CONFIRMED, not reproduced.** The five
`SSH host key error` hosts within minutes of her 0.4.1 upgrade match the v1-reconciler
ordering-cycle history: a VM reboot replays bake cidata over the SSH material, the served
key reverts to bake-origin, and strict user-origin pins refuse. Not self-healing:
`_verify_and_heal` deliberately raises `HostKeyDriftError` (remedy:
`mngr imbue_cloud hosts rotate`) rather than re-trusting. Reproducing live would mean
deliberately reverting served keys on a production host - out of authorized scope.

## New findings the handoff did not have

**N1 - during the Aug 20 hangs, the workspace was fine.** Seconds after each 1260s timeout
the health probe succeeded (`10:23:32 timed out` -> `10:23:35 restart_failed -> HEALTHY`),
and twice the probe flipped HEALTHY while the start was still hanging. The start pipeline
was the outage; the recovery machinery was hostage to a doomed 21-minute subprocess while
the workspace it was "recovering" answered HTTP.

**N2 - no client-side liveness bound anywhere in mngr's SSH stack.** No
`transport.set_keepalive()` call exists in mngr (only the lima CLI passes
`ServerAliveInterval`). pyinfra's `CONNECT_TIMEOUT=10` bounds TCP connect and
`banner_timeout=30` bounds the banner, but command execution has no default timeout: the
exec/read path (`recv`) blocks forever on a wedged transport. A start makes ~25-30 such
round trips, each an indefinite-hang exposure point. Kanjun's app-side logs show her box in
exactly that state for 7+ continuous hours on Aug 20 (channel opens timing out at the
transport level; zero "refused", zero "network unreachable"), which is sufficient to
explain her 1260s burns without any lock contention.

**N3 - container sshd is unhardened relative to the VM sshd.** `ClientAliveInterval 0` vs
120. Everything that talks to the container (the host lock channel, execs, tunnels) leaves
~2h server-side debris when a client path dies silently; the VM cleans up in 6 minutes.

**N4 - healthy-path waste.** Triple agent loading, two separate outer connections for the
same information, discovery visiting hosts unrelated to the start target, ~3s of
interpreter/import per invocation, resume-message machinery running a full `get_agents`
even when no resume message exists. None of it explains a hang; all of it scales the
"normal" start from 13s toward 60s+ on high-RTT or many-host setups.

**N5 - testing footgun (for the record).** Running `mngr` by hand against the minds host
dir without `MNGR_PREFIX=minds-` targets different tmux session names: the first baseline
run started a duplicate `system-services` under `mngr-system-services` on the production
host. Production minds superseded it ~19s later; no lasting damage, but manual repro
must set the prefix.

## Second pass: time-of-day analysis + read-only server-side audit (2026-08-20, later)

Gabriel challenged the "her box was flapping" attribution, and it did not survive. Two
further passes -- a sleep-aware re-read of her client logs, and a read-only pass over her
actual boxes (vault role `minds_production`, pool DB + pool SSH key, credentials deleted
after) -- rewrote the story:

- **The Aug 20 1260s timeouts were her laptop sleeping, not the box.** All four fall
  between midnight and 7:21am her time, inside a window whose minds.log shows the classic
  macOS closed-lid pattern: total log silence in back-to-back ~15-18 min blocks with brief
  dark-wake bursts between. Each restart was dispatched during a wake, the laptop slept
  ~20s later, and the 1260s budget burned across the sleep cycles. The "workspace answered
  probes seconds after each timeout" observation from the first pass now reads correctly:
  the box was fine; the laptop had just woken up.
- **Her box was healthy the entire time (server-side, conclusive).** VM for her
  `system-services` workspace (51.81.154.73:22018): no OOM, no kernel warnings, load
  ~0.3-0.5, sshd accepting throughout the window -- including at the exact dispatch minute
  of the first overnight start -- and zero dead-peer "client not responding" events. Inside
  the container, the system-interface python processes ran continuously from 05:55Z through
  her whole morning of "Lost connection" episodes; the container sshd was up since boot.
  Nothing on the server corresponds to any of her episodes.
- **The Aug 19 "SSH host key error" burst was a fleet-side mass VM reboot.** All five of
  her slice VMs rebooted that evening in two waves (~22:53Z and ~23:38Z), and each of her
  five client-side key errors matches its VM's boot time to the minute (cidata replay
  reverting keys under the v1 reconciler). Her 0.4.1 upgrade at 22:45Z the same minute is
  probably reverse causality: the mass disconnect prompted an app restart, which
  auto-updated. Three of those hosts stayed stranded until the server-side rescue at
  ~17:00Z Aug 20 (see the fleet host-key audit notes).
- **What she actually experienced while looking at the machine** (7:21-9:02am her time):
  brief client-side tunnel blips (~1 min of channel-open failures as transports
  re-established), each escalated by the health tracker to STUCK after just **8.0 seconds**
  of probe failures, which dispatches the full host-restart pipeline with its 1260s-capped
  start. Her bug report ("Lost connection to the machine, and is reconnecting") was filed
  53 seconds after one such dispatch. The heavyweight, slow-to-resolve recovery response to
  sub-minute blips *is* the experienced bug.
- **The zero-rows mystery is resolved: an upload-coverage gap.** The events.jsonl in the
  bug bundle is the latchkey forward supervisor's own `--log-file` (a separate file under
  the latchkey plugin dir -- see `mngr_latchkey/_spawn.py`), not the per-command mngr
  events file at `<host_dir>/events/logs/mngr/events.jsonl`. The real file, with the
  hanging starts' DEBUG timelines, was never collected and is presumably still on her
  laptop.

Net effect on attribution: the box-wedge theory is retracted; the 1260s burns were
laptop-sleep artifacts and the attended-time pain was short client-side blips amplified by
the recovery machinery. The server-side infra issue that was real -- the mass reboot plus
v1-reconciler key reversion -- is a separate, known failure mode with its own fix history.

## What this means for a fix (constraint: no unattended-mode timeout inside `mngr start`)

The hang decomposes into three independent mechanisms, each with a mechanism-level fix that
makes start fail honestly instead of capping it from outside:

1. **Transport liveness bounds in mngr's SSH layer** (primary). paramiko keepalives
   (~15-30s) on every connection (pyinfra connector, `ParamikoSliceVmAccess`, outer hosts),
   a default per-command exec timeout (generous, overridable for known-long commands), and
   a channel-open timeout. A wedged transport becomes a named error in ~1-2 minutes
   ("command X on host Y stalled") on every path, attended or not. This addresses N2, the
   dominant explanation for Kanjun's Aug 20 hangs, and also unwedges the create-holds-lock
   case (a wedged create dies and releases instead of holding forever).
2. **Container sshd `ClientAliveInterval`** (bake + reconciler heal), e.g. 30s x 4.
   Ghost holders and dead tunnels die in ~2 minutes instead of ~2h11m. Addresses N3/F3.
3. **Lock wait diagnosability.** Record holder metadata (pid, argv, timestamp) into the
   lock dir under the flock at acquire time; when a start waits, log WHO it waits on at
   WARNING with elapsed time. Optionally a large finite default (e.g. 10 min) for start's
   lock wait producing a diagnosable `LockNotHeldError` naming the holder - not
   mode-specific. (Needs discussion; create keeps None.)
4. **Minds-side capture** (cheap, high value). Keep the timed-out subprocess's captured
   stdout/stderr (last N lines in the `MngrCommandTimeoutError` message -> Sentry), and
   include `<host_dir>/events/logs/mngr/` in the bug-report bundle (the second pass showed
   the bundle currently uploads only the latchkey forward's separate log file).
5. **Sleep/blip awareness in the recovery machinery** (elevated by the second pass -- this
   is what Kanjun actually experienced). The tracker flips STUCK after 8s of probe
   failures and dispatches a 1260s-capped host restart; overnight it does this repeatedly
   across the laptop's own sleep cycles. Candidates: detect a sleep/wake transition (wall
   vs monotonic jump) and re-probe before dispatching; require a longer failure window
   before the *restart* escalation (the probe can stay twitchy for UI purposes); do not
   count an in-flight start's budget across sleep. This is minds-side policy, not an
   `mngr start` timeout, so it respects the constraint.
6. **Healthy-path cost** (optional, separable): reuse one SSH connection per start, batch
   the per-agent reads, skip resume-message loading when unset, pin discovery to the
   target provider/host.

Not proposed: any timeout special-cased on unattended mode (explicitly rejected), and any
change to the 1200s restore poll (it is a genuine restore budget and is never reached on
the running-host path).

## Implemented (this branch)

- **SSH keepalives everywhere** (`SSH_KEEPALIVE_INTERVAL_SECONDS = 15`): set on every
  pyinfra host connection at the `_ensure_connected` chokepoint (covers `Host` and
  `OuterHost`) and on `ParamikoSliceVmAccess` connections. Bounds the dead-path case.
- **Channel-open and SFTP bounds**: the host-lock channel, the detached debug holder, and
  SFTP channel creation open with a 30s timeout (previously unbounded -- the exact
  "accepts TCP, ignores channel opens" signature from the incident); SFTP channels get a
  300s default per-read silence timeout.
- **Container sshd `ClientAliveInterval 30` / `ClientAliveCountMax 4`**, via
  `SSHD_START_OPTIONS` in `ssh_host_setup.py`. This turned out to be a monorepo fix, not a
  default-workspace-template one: mngr itself launches every container sshd (initial
  provisioning, `start_host` relaunch, the self-healing entrypoint, and Modal's sandbox
  `exec`) with these `-o` flags, which override any image-baked sshd_config -- so all
  providers and *existing* containers converge on their next sshd relaunch, no bake or
  fleet sweep needed. (Modal was launching sshd from its own copy of the options and had to
  be pointed at the shared constant.) The *outer* VM sshds are out of scope here -- they are
  configured at bake/cloud-init time, and the one measured (workspace-1's) already runs with
  `ClientAliveInterval 120`.
- **Minds keeps the failed subprocess's output**: bounded stdout/stderr tails ride
  `MngrCommandError.output_tail` into the single restart-failure error record; the recovery
  argv passes `-v` instead of `--quiet` so that tail contains the step timeline; and the
  bug-report sweep now uploads the per-command mngr events log. In support,
  `concurrency_group` now drains a killed process's final output into `FinishedProcess`
  (previously anything written between the last poll and the kill was lost). The `-v`
  timeline stays *on* that tail: the error message is narrowed to mngr's verdict block (from
  its last `Error:`/`ERROR:` marker on), because `str(exc)` is both the user-facing
  restart-failure text and the input to two string classifiers -- and at DEBUG mngr logs
  every provider it skips as unavailable with the verbatim `ProviderUnavailableError` text
  the outage parser matches, for a provider it then continued past.

**Found along the way, deliberately not fixed here: pyinfra's per-command `_timeout` is
inert in mngr's usage.** pyinfra enforces it via `gevent.wait` over reader greenlets that
do blocking paramiko reads, and mngr does not gevent-monkeypatch, so a blocked read starves
the hub and the timer can never fire mid-command (verified live: `sleep 20` under
`timeout_seconds=1` ran to completion). This means existing `timeout_seconds=` bounds on
*remote* commands (e.g. `stop_agents`' per-command budgets) do not actually bound a
silent hang -- they only fire if reads yield. A real wall bound would need a worker-thread
wrapper plus forced disconnect (or replacing pyinfra exec); deferred until a failure mode
that needs it is actually observed, since keepalives + open bounds cover every mechanism
in evidence.
