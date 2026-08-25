# Live verification of the CONNECT_ERROR decomposition

Run against the `dev-gabriel` env on macOS, driving the real Electron client over
CDP (`just minds-start "" "" 9223`), with the disposable docker workspace
`deckertest` (`agent-07bf9f7647894911a5ed2262cc09e34b`, container
`minds-dev-gabriel-deckertest`). Every classification below came out of
`~/.minds-dev-gabriel/logs/minds.log`; every copy claim came out of the rendered
DOM of the running app.

## What holds

* **`BACKEND_NOT_LISTENING` against a real sshd.** Stopping the in-container
  `system_interface` service (sshd untouched, port 8000 refusing) produced
  `classified as BACKEND_NOT_LISTENING: Server disconnected without sending a
  response.` The only accompanying warnings were paramiko's
  `Secsh channel N open FAILED: Connection refused`; no
  `Failed to open an SSH channel ... at the transport level` line appeared, so
  `_is_transport_unusable` read the refusal as leaving the transport usable, as
  its docstring claims. This was the one classification with no end-to-end
  coverage.

* **`TUNNEL_SETUP_FAILED` against a real tunnel rebuild.** With `known_hosts`
  moved aside and the container's `sshd-session` processes killed to force a
  reconnect, the forward reported
  `classified as TUNNEL_SETUP_FAILED: No known_hosts file at ...`, and the same
  reconnect logged `Failed to open an SSH channel ... SSH session not active` --
  a genuinely dead transport was read as dead, and a refused channel over a live
  one was not. Both arms discriminate correctly on the same machine minutes
  apart.

* **The device-side card, end to end.** With the missing `known_hosts` made
  persistent (the keys directory held read-only so the dispatched start could not
  regenerate it), `recovery-info` reported `is_device_cannot_connect: true` while
  `health` was `restart_failed` -- the verdict outranks the restart episode. The
  notice band read `Can't connect to this machine from this device`; the card
  read `Can't connect to deckertest from this device`, offered `Restart Minds`
  and `Report a problem` with **no Restart Machine**, and its `Error details`
  disclosure expanded to the verbatim path.

* **Clearing on the evidence, without the machine having been restarted.**
  Restoring `known_hosts` took the machine `restart_failed -> HEALTHY (probe
  succeeded)`, and nothing was booted in between. Note what this does and does
  not show. The unattended dispatch still fired on the STUCK edge
  (`Unattended recovery ... DISPATCHED`) -- by design, since the plan keeps
  dispatch evidence-free and changes only what the surfaces claim -- and in this
  run its start step then errored on the read-only keys directory the harness
  itself had created. So this run is evidence that the *verdict and the copy*
  follow the trust material rather than the machine, and that recovery needs no
  boot; it is not evidence that the dispatch is suppressed, because it is not.

* **`was_host_started`, both arms, over the real dispatch.** Against a running
  host: `Start step of host restart ... booted nothing; the host was already up`,
  card log line `The machine was already running; it was not restarted.`, badge
  `Not responding` and card heading `deckertest unresponsive` once the 300s
  budget expired. Against a host stopped from outside the app: no "booted
  nothing" line, and recovery in 12s. No
  `mngr start reported no was_host_started` warning ever appeared, so the
  `--quiet --format json` stdout contract holds for the docker provider.

* **Restart framings.** Unattended start-only dispatch: card
  `Reconnecting to deckertest...` and machines-list badge `Reconnecting...`,
  agreeing. User-initiated Restart Machine: card `Restarting deckertest...`
  immediately on click, with `recovery-info` confirming
  `health=restarting, is_restart_start_only=false`, which is what the badge
  selector reads.

* **Probe deletion.** `GET /api/v1/workspaces/<id>/health` returns 404.
  `$MNGR_HOST_DIR/plugin/forward/service_map.json` is present and instance-keyed,
  and the app started routed (no routeless multi-minute load).

## What did not hold, and was fixed

Every item below was found by the run above and fixed on this branch; the
re-verification at the end of this file is against the fixed build.

### The machines list still blames the machine when this device is at fault

`healthBadgeLabelFor` takes `(health, isRestartANoOp, isRestartStartOnly)` and
never reads `is_device_cannot_connect`, even though the server puts that field on
the same workspace entry the row renders and `Shell.ts` already reads it for the
notice band. Observed simultaneously on one machine: card
`Can't connect to deckertest from this device` with Restart Machine withheld,
landing row badged `Restart failed`.

This is the sibling review's finding 1 one step later -- fixed for `restarting`,
still open for the terminal states -- and the plan states the verdict outranks
the restart episode on the card, the notice band, *and* the machines list.

**Fix:** `healthBadgeLabelFor` takes the verdict and ranks it above every reading
below it, badging `Can't connect from this device`.

### A dead auxiliary service logs a classification line forever on a healthy machine

`_emit_backend_failure` is keyed by agent, not by service, so any registered
service that stops listening emits `BACKEND_NOT_LISTENING` for the *agent*. Here
a stopped dev server on port 8080 (an open workspace tab) produced, indefinitely:

```
System-interface connection failure for agent-... classified as BACKEND_NOT_LISTENING: ...
Enrolled agent-... as a system-interface probe suspect (backend-failure envelope)
```

one pair every 2 seconds, with the machine reading HEALTHY throughout. Starting
any listener on 8080 took the rate to zero; stopping it resumed it. The
per-cause dedup in `record_connection_failure` never engages because each
intervening probe success pops the episode record, so the "different cause
replaces it, which is the only case worth a log line" reasoning does not survive
contact with a healthy machine that has one dead service.

The envelope and the enrollment churn are pre-existing; the INFO line and its
Sentry breadcrumb are new here. At ~1800 lines/hour per affected machine the
breadcrumb stream drowns the device-side signal it was added to measure.

**Fix:** the log is rationed against a mark that a probe success does not drop,
so one cause is written at most once per `connection_failure_log_interval_seconds`
(300s) per agent while a different cause is still written at once. The record the
surfaces read is written on every envelope, exactly as before. The enrollment
churn is left alone: probes are the arbiter by design and one costs a request.

### The card contradicts itself while a start-only restart is in flight

Heading `Reconnecting to deckertest...`, primary button `Restarting...`
(`RecoveryCard.ts`, `isBusy ? "Restarting..." : "Restart Machine"`). The string
predates this branch, but so did the heading that used to agree with it.

**Fix:** `recoveryBusyActionLabel` reads the same evidence `recoveryHeading`
does, and a test pins the two against each other over every combination of it.

### The device-error disclosure prints the same path twice

`_create_ssh_client`'s `checked_paths` assumes the explicit `known_hosts_path`
differs from the key sibling. The docker provider sets it to the same file, so
the real card read:

```
No known_hosts file at /Users/.../docker/keys/known_hosts or /Users/.../docker/keys/known_hosts; refusing to connect without a pinned host key
```

**Fix:** the two candidates are deduplicated before the message is built.

### Literal `--` in card copy

`This machine may be running normally -- the connection failed on this device,
before reaching it.` renders as two hyphens in the card body.

**Fix:** an em dash, matching the app's other prose copy.

### `POOL_EXHAUSTED` would show a bare class name

`_describe_backend_failure_cause(httpx.PoolTimeout(""))` returns `'PoolTimeout'`
(`ReadError` likewise), so the card's `Error details` would read
`PoolTimeout`. Verified by calling the function directly, not by exhausting the
pool live.

**Fix:** `PoolTimeout` alone gets a description, because `POOL_EXHAUSTED` is the
only message-less case whose detail a person reads -- every other one reaches the
log, where the class name is the right amount of detail.

## Not covered here

* Pool exhaustion driven live (needs `_TUNNEL_POOL_LIMITS` lowered and a relaunch).
* Linux: the `ReadError`-vs-`RemoteProtocolError` split is platform-dependent and
  only macOS was exercised.
* The `imbue_cloud` provider: `was_host_started` was proven over docker only.
* `Bringing <name> back online...`: on docker, discovery never reported the host
  as offline before the dispatched start had already booted it, so
  `is_host_offline` stayed false for the whole outage and that copy branch was
  never reached.

## Re-verification after the fixes

Same environment, same workspace, against the fixed build.

* **The machines list carries the verdict.** With `recovery-info` reporting
  `health=restart_failed, is_device_cannot_connect=true`, the landing row badges
  `Can't connect from this device` -- where it previously badged
  `Restart failed` under a card that had withheld Restart Machine.

* **The log is rationed.** With the same dead service on :8080 and the workspace
  open, 32 refused channel opens over ~70s produced exactly 1 classification
  line, and a 90s window produced 1 (the staged `TUNNEL_SETUP_FAILED`, a
  different cause, which is not rationed). Before the fix the same condition
  produced one line every 2 seconds. The `Enrolled ... as a probe suspect` DEBUG
  line still fires per envelope: that churn is pre-existing, and each enrollment
  costs one probe, which is the arbiter this design wants.

* **The card agrees with itself.** Start-only dispatch: heading
  `Reconnecting to deckertest...`, button `Reconnecting...`. User-initiated
  Restart Machine on the same machine: heading `Restarting deckertest...`,
  button `Restarting...`.

* **One path, and an em dash.** The expanded `Error details` now reads
  `No known_hosts file at /Users/.../docker/keys/known_hosts; refusing to connect
  without a pinned host key`, and the body reads `running normally — the
  connection failed on this device`.
