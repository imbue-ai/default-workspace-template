# Environment signals: sleep and connectivity awareness for minds

## Overview

- Minds has zero awareness of sleep or connectivity today, so laptop-side conditions (sleep, dark-wake bursts, dead wifi, SSH-blocking networks) are convicted as workspace death: phantom STUCK verdicts at wake, unattended `mngr start`s doomed to DNS failures, RESTART_FAILED + error-level Sentry reports whose log uploads also fail, and spurious discovery-producer bounces on the first post-wake tick.
- Add two backend detectors to the desktop client: a heartbeat-gap sleep tracker (wall-clock gaps in a ~1s tick) and a demand-driven connectivity detector with two distinct facets: internet and SSH reachability.
- The heartbeat design rests on a measured fact: `time.monotonic()` freezes during macOS sleep on Apple Silicon (two ~15-min lid-closed sleeps advanced it 1-3s), so wall-clock gaps cleanly mark not-running intervals and the monotonic deadlines this branch sets already extend across sleep on their own. No per-platform clock machinery, no Electron `powerMonitor` dependency. That reasoning covers monotonic deadlines only: the restart subprocess's own deadline is stamped in wall clock and a sleep does spend it, which the second findings section measures.
- Binding semantics from the recovery-work-principles contract: signals only suppress negative verdicts or gate actions, never assert health; an "unknown" reading never suppresses; workspaces on local backends (`local`, `docker`, `lima`) are wholly exempt from connectivity gating (sleep hygiene still applies to them — the probe loop itself was frozen).
- STUCK remains the single recovery vehicle: it still fires truthfully, and the connectivity reading taken on its edge is what withholds the unattended dispatch. What the withholding leaves behind is the machine's membership in the owed set; the recovery card/band copy reads the device's condition off the detector's cached reading, the same source every other consumer uses. (An earlier build also recorded a per-machine qualifier on the health state; the decisions section records why it went.)

## Expected behavior

Sleep:

- A failure run that straddles a recorded sleep interval resets its onset: the 5s STUCK threshold must accumulate entirely while awake, so no part of a conviction is ever backed by seconds during which nothing was probing. That is the whole of what the reset buys, and it is narrower than "no recovery card at wake" -- a machine that is genuinely unreachable for 5s *after* the lid opens is convicted as it always was, which the findings section below shows is what actually happens, and for a cause this branch does not address.
- The discovery watchdog no longer reads a >180s sleep as a producer stall: staleness re-baselines to `max(last_event_at, started_at, last_wake_at)` and the remediation episode state (bounce flag, restart backoff) resets on wake — no spurious SIGHUP bounce or supervisor-restart escalation on the first post-wake tick.
- A workspace that died during sleep is still auto-started ~7s after it is first probed post-wake; there is deliberately no post-wake settle window (the awake-only failure run, the offline gate, and start-only idempotence cover what a settle would, without taxing the dead-workspace case).
- Dark wake is handled naturally: each powernap burst reads as a short awake window between sleep intervals, so `last_wake_at` tracks burst starts and convictions inside a burst still require 5s of in-burst failures.

Connectivity:

- A remote-provider workspace that stops answering still goes STUCK, but the stuck edge now triggers a connectivity probe; on confirmed offline or SSH-blocked, the unattended start is withheld and recorded as owed.
- While an environmental state is active the detector fast-polls; when connectivity returns, the owed start fires if the workspace is still STUCK. No steady-state background probing ever; a wake only invalidates the cached reading.
- The recovery card and notice band show distinct environmental states: "offline" vs "incompatible network" (SSH blocked — the user's browser works, so they must not be told they're offline). An app-level indicator accompanies the per-workspace state, and does not wait for a workspace to be convicted before it speaks.
- While confirmed offline/SSH-blocked the card replaces the restart button with a waiting-for-network state; the button returns when the state clears (a shown button must always restart unconditionally).
- SSH-blocked is declared only when the whole quorum of independent public SSH endpoints fails the banner check while the internet facet is up; "unknown" (probes not yet conclusive, e.g. right after wake) behaves as online.
- A restart already in flight that fails while the device is confirmed blocked -- offline, or on a network that blocks SSH -- keeps RESTART_FAILED (truthful state, user-retryable) but logs at warning — no error-level Sentry report firing doomed log uploads.
- The probe-success path is unchanged: the workspace answering flips everything back to HEALTHY and the card auto-returns the user.
- Local-backend workspaces behave exactly as today while offline: their probes, convictions, and dispatches are untouched.
- Telemetry is local-log-only: structured lines for sleep intervals (with the wall-vs-monotonic delta as a gap label), facet transitions, and gated/owed dispatches. No Sentry events for environmental states; the lines reach bug-report bundles via the normal log sweep.

## Implementation plan

New module `apps/minds/imbue/minds/desktop_client/environment_signals.py`:

- `SleepTracker(MutableModel)`: a heartbeat thread on the root `ConcurrencyGroup` (~1s tick sleeping on `shutdown_event.wait`, matching the probe-loop pattern in `app.py`). Each tick records wall time; a tick-to-tick wall gap above a threshold (~30s, comfortably above GIL/load hiccups) records a `[gap_start, gap_end]` interval. The gap log line carries the wall-vs-monotonic delta as a diagnostic label. Public surface as shipped: `was_asleep_since(t0)`, `get_last_wake_at()`, `add_on_wake_callback(cb)` (callbacks fired outside any lock, like the existing trackers). Injectable clocks for tests.
- `ConnectivityFacet` enum (`ONLINE`, `OFFLINE`, `UNKNOWN`) and `ConnectivityDetector(MutableModel)`: cached facet readings (`internet`, `ssh`); `probe_now()` as the demand trigger (shipped name); a TCP connect for `internet`, and for `ssh` a banner check (`SSH-2.0` prefix) against the endpoints minds itself dials, with the public quorum as the tiebreaker (see the decisions section). While any facet is confirmed OFFLINE a fast-poll loop (~5s) runs until recovery, then goes quiet. `add_on_recovery_callback(cb)` for owed-dispatch re-fire. Wired to `SleepTracker` on-wake to invalidate the cache to UNKNOWN. Injectable prober for tests.
- `is_network_dependent_workspace(backend_resolver, agent_id) -> bool` (shipped in `workspace_recovery.py`, see the decisions section): provider-locality helper keyed off the discovered provider's `config.backend` (`local`/`docker`/`lima` exempt; unknown backend counts as network-dependent, the conservative direction for gating but never for suppression).

Modified `apps/minds/imbue/minds/desktop_client/system_interface_health.py`:

- `SystemInterfaceHealthTracker` gains an optional injected `SleepTracker`. `record_probe_failure` resets `failure_run_started_at`/`_wall_at` to now when `was_asleep_since(failure_run_started_wall_at)`.

Modified `apps/minds/imbue/minds/desktop_client/workspace_recovery.py`:

- `UnattendedRecoveryDispatcher.__call__`: for network-dependent workspaces, trigger `probe_now()` and obtain a reading (the stuck-edge callback must stay fast — the wait happens on a small worker, not the probe thread); confirmed-bad → record the agent as owed, skip the dispatch; UNKNOWN/ONLINE → dispatch as today. A connectivity-recovery callback fires owed dispatches for agents still STUCK. This owed-set is minimal and self-contained (no dependency on PR #375's dispatch-history machinery; whichever lands second reconciles the two).
- `run_restart_sequence`: when either restart step fails while the detector reads a confirmed device-level block (OFFLINE or SSH_BLOCKED), log the failure at warning instead of error (state still becomes RESTART_FAILED). Both steps route through one reporting helper; see the decisions section for why the stop step is included.

Modified `apps/minds/imbue/minds/desktop_client/discovery_health.py`:

- `DiscoveryHealthWatchdog.evaluate` accepts the wake baseline: staleness ages from `max(last_event_at, _started_at, last_wake_at)`; a wake newer than the last evaluate resets `_bounce_attempted`, `_restart_count`, and `_last_remediation_at`.

New module `apps/minds/imbue/minds/desktop_client/workspace_view_refresh.py` (not in the plan as written, see the decisions section):

- `WorkspaceViewRefresher`: the health tracker's recovery callback, moved out of `app.py` (it was `_WorkspaceViewRefresher` there) and given the two things it needs to decide *when* to publish -- the detector and the backend resolver. A refresh for a network-dependent machine raised while the device is blocked, or within a 5s settle of a block lifting, is held and published at the end of that settle; everything else publishes at once, as before.

Modified `apps/minds/imbue/minds/desktop_client/app.py`:

- Construct and wire both detectors in `create_desktop_client` / `run`; inject the `SleepTracker` into the health tracker and into both loops that read the wake -- the discovery watchdog and the system-interface health probe, each of which ticks it itself rather than racing the heartbeat thread for it on resume; publish the app-level environment state through the UI-state publisher.

Frontend (`apps/minds/frontend/src/`):

- `views/recovery/RecoveryCard.ts`: two new states (offline, incompatible network) with the restart-button-to-waiting swap; `views/shell/notice-band.ts`: environmental copy variants; the app-level indicator on the shell; model/type updates for the extended health payload.

Docs:

- Correct the wrong "monotonic advances during sleep" claim in `subsystems-and-recovery.md` when `gabriel/recovery-audit` lands; the spec cites the environment-signals contract in `recovery-work-principles.md`.
- Changelog entries per touched project.

## Implementation phases

1. **Sleep awareness (PR A)**: `SleepTracker` + the failure-run reset + the watchdog re-baseline, wired and tested. Working result: no phantom convictions or producer bounces at wake; dead-since-sleep workspaces still auto-start.
2. **Connectivity backend (PR B, part 1)**: `ConnectivityDetector` + locality helper + dispatch gating/owed re-fire + the offline restart-failure downgrade. Working result: no doomed offline dispatches or Sentry storms; card still shows today's generic copy.
3. **Connectivity surfacing (PR B, part 2 — same PR or stacked)**: the two card states, band copy, app-level indicator, button swap, payload plumbing. Working result: users see "offline" / "incompatible network" instead of "restarting" lies.

## Testing strategy

- Unit, with injected clocks/probers (fixtures per the shared-conftest conventions):
  - `SleepTracker`: simulated tick gaps produce intervals; `was_asleep_since` boundary cases; wake callbacks fire once per gap; negative wall deltas (clock stepped back) ignored.
  - Failure-run reset: a run whose onset predates a sleep interval re-accumulates from wake; a run entirely awake convicts unchanged; force-`mark_stuck` (no onset) unaffected.
  - Watchdog: no stall on the first post-wake tick after a long gap; episode counters reset on wake; existing stall/backoff tests unchanged.
  - `ConnectivityDetector`: quorum logic (one SSH host up → not blocked; all down + internet up → SSH_BLOCKED; internet down → OFFLINE, not SSH_BLOCKED); UNKNOWN after wake until a probe lands; fast-poll stops on recovery; recovery callbacks fire.
  - Dispatch gating: confirmed-bad → owed + no dispatch; UNKNOWN → dispatches; local-backend workspaces never gated; owed dispatch fires on recovery only while still STUCK; destroyed/recovered agents drop out of the owed set.
  - Restart worker: offline start failure logs warning, still RESTART_FAILED.
- Frontend: recovery-card tests for the new states and button swap (extend `recovery-card.test.ts`); notice-band copy selection.
- Manual verification protocol (CI cannot sleep the machine; documented in the spec): a real sleep cycle → interval logged, no bounce/conviction at wake; wifi off with a remote workspace → offline card, withheld dispatch logged; wifi on → owed start fires and the card returns; SSH-blocked simulation (block outbound 22, e.g. a pf rule) → incompatible-network state while browsing works.
- Edge cases to cover in review: app start immediately after wake (no heartbeat history → no intervals, UNKNOWN facets → no suppression anywhere); multiple sleeps inside one failure episode; quit with owed dispatches pending (per-process state, dies with the app — acceptable); the stuck-edge probe wait must not block the probe-loop thread.

## Decisions taken during implementation

- The SSH facet measures the endpoints minds itself dials, not port 22. The plan's public-quorum-on-22 design was wrong for this codebase: imbue_cloud machines are reached on a box-forwarded port in the 22000-32000 range (`DEFAULT_SLICE_PORT_RANGE_START`/`END`), so :22 answers a question about a port those machines never use -- in both directions. A network blocking :22 while permitting high ports would have produced an incompatible-network verdict, and withheld the restart, on a network where minds works fine; a network permitting :22 while blocking the forwarded range would have reported itself healthy and dispatched every doomed restart. Discovery already reports the real coordinate per agent (the same one the recovery card renders as its `ssh` command), so the detector takes a callable supplying them and asks those first, capped at three.
- The public quorum (`github.com`, `gitlab.com`, `bitbucket.org` on :22) survives as the tiebreaker for the one case minds' own endpoints cannot settle: every one of them failing is either this network blocking SSH or those machines being unreachable in their own right, and a public host still serving a banner says it was the machines -- so the facet stays ONLINE and recovery goes on treating it as a machine problem. Not settings-overridable: three independent operators already make a single site's outage a non-verdict, and a knob nobody can be told to turn is worse than a constant that is easy to change.
- Known residual gap: a network permitting :22 while blocking the forwarded range still reads healthy, since the tiebreaker believes the public host. That degrades to the pre-existing behaviour (dispatch and fail) rather than to a wrong verdict, so it is accepted.
- Residual, and the one that has no end: the fast poll stops at the first good reading, which a dropped wifi produces within minutes -- but a network that blocks SSH does not change on its own, so a single SSH_BLOCKED verdict on a corporate or hotel network leaves a round every 5s for the rest of the session, and this window measured a round at 9.25s. That is what "while an environmental state is active the detector fast-polls" asks for; what is new is that the state can have no end, which the expected-behaviour line about never probing in steady state does not cover. The lever is to back the interval off while the reading does not change, and it costs the thing the 5s was chosen for: how long an owed restart waits after the network returns. Not pulled here -- which of the two waits matters more is a product call, and the evidence for it (how long a real SSH-blocked session lasts) is not in this window.
- The mirror of that gap is worse and is mitigated rather than accepted. On a network blocking :22 while permitting the forwarded range, a machine whose host has *genuinely died* fails every sampled endpoint, the public quorum fails too, and the reading is SSH_BLOCKED -- so the start is withheld, the reading never improves (the network never changes), and the card offers no restart. The sample therefore asks endpoints on hosts discovery reports as RUNNING first: a stopped or crashed host cannot answer whatever the network is doing, and spending a bounded sample on it is what sends the facet to the quorum. Ordering rather than filtering, because on a dead network discovery goes stale too and a reading taken when nothing is known to be running must still be able to ask about something.
- Copy: the band says "No network connection." / "This network blocks the connection to your machines."; the card leads with "<name> can't be reached from this device." and explains which condition holds, naming SSH and conceding that the browser works. Neither surface vouches for the machine. An earlier draft reassured the user their machines were still running, which is a claim about the far side of a connection nothing here measured -- and a wrong one for a machine that died just before the network did. The offline copy promises only what minds can keep: that it is watching and will say so when it can see again.
- The restart affordance is *replaced* by a non-interactive "Waiting for network..." line, not disabled: a shown button must always restart unconditionally, and a restart routed over the same dead network is not a decision worth offering.
- The device's condition is published as app-level state (`UiEnvironmentMessage`, beside `UiDiscoveryHealthMessage`), and *only* as app-level state. Deriving it from per-machine records -- the shape this shipped with first -- had a hole big enough to matter: an app opened on a dead network has nothing to convict until the user clicks into a machine, so the hub pages they are actually looking at said nothing at all. A second shape recorded a per-machine qualifier on the tracker beside the app-level fact, so the card and band could keep explaining across the window in which a wake blanks the reading to UNKNOWN. That stickiness stopped paying for itself once the CONNECT_ERROR decomposition (merged from main) reframed the fallback copy: the window now renders "Reconnecting to <machine>..." -- which is true -- for the few seconds until the watching loop's next probe re-establishes the condition, and everything else the qualifier carried duplicated the detector's reading. Removing it deleted the tracker's record/clear/get machinery, the qualifier's field on the health frame and the recovery-info route, and the withhold-vs-restart write races that machinery had to reason about. The cost is that post-wake window: a Restart button shown for a few seconds on a still-dead network routes a start over it and lands as an ordinary warning-level RESTART_FAILED.
- The connectivity probe therefore has a second trigger beside the STUCK edge: a network-dependent provider that discovery reports as unreachable. That is the earliest evidence a cold start on a dead network produces -- discovery's first poll fails immediately -- and it needs no machine to have failed. Local-backend providers are ignored (a stopped docker daemon errors the same way and says nothing about the network), the probe runs on a worker, and it measures once per error episode -- keyed on the set of errored network-dependent providers, so a second one going dark is new evidence -- rather than once per event, which would have made a provider that stays broken into a permanent network poll.
- App-level indicator: a notice-band / in-page-notice variant, no new chrome. The per-workspace band carries it over a displayed machine, and `localPageNoticeFor` carries it into hub pages (which have no band). The recovery card reads the same condition through the recovery-info route, so the card behind the band's "Open recovery" explains the same condition the band named.
- The app-level fallback is locality-scoped on the two per-machine surfaces, which the shape this shipped with first was not. The gate exempts on-device workspaces, but the card and band read the device's own condition with no such check -- so a wedged docker container, while the wifi happened to be off, was narrated as unreachable-from-this-device and had its restart button replaced by "Waiting for network...", withholding on the surface the very dispatch the backend had been careful not to withhold. The recovery-info route now answers `device_environment` through the same `read_environment_block` helper the restart-failure path uses (NONE for a machine the network cannot explain), and the workspaces frame carries `is_network_dependent` per row so the band can decline to speak over one. The hub-page notice is deliberately unscoped: it is a statement about the device, not about any machine.
- A recovered machine's view refresh is held until the network has been back for 5s, which the plan above did not anticipate and which is not the post-wake settle line 17 declines. The two differ in what they are waiting out: line 17 is about a *verdict*, and the awake-only failure run already covers it; this is about a *reload*, which the verdict machinery says nothing about. The first probe that can succeed is the first moment the interface is back, and that is exactly when the browser invalidates every in-flight socket -- the reload commits its document and then loses the scripts that would have booted the page, leaving a blank frame that reads as healthy from every angle the app can see. Measured either side of it on one laptop across two wifi cycles: published 0.2s after the interface returned, the reload lost its scripts to `ERR_NETWORK_CHANGED` and the frame stayed blank; published 18.9s after, it loaded cleanly. Five seconds sits in that bracket, past the burst of interface notifications a reassociation emits, and is not a delay a user can pick out of the ~15s of probe failures it already took to call the machine stuck. Only network-dependent machines are held, for the reason everything else here is locality-scoped and one more besides: the release is the network coming back, which for an on-device machine may never happen. This narrows the window rather than closing it -- what makes a lost reload recoverable is the embedder noticing the frame it armed never came up, which is the frontend's job.
- The dispatch waits for the reading on a one-shot worker thread spawned from the stuck-edge callback, not on the probe-loop thread and not by deferring the decision. Nothing is lost by the delay: the machine is already stuck and `mngr start` is idempotent.
- The locality helper `is_network_dependent_workspace` lives in `workspace_recovery.py` rather than in the new module, which the plan above placed it in. It needs the backend resolver, and `environment_signals.py` is otherwise a leaf that knows nothing about discovery: importing the resolver there to host one predicate would have coupled the detector to the thing it is asked about. Its callers -- the gate, the restart-failure report, the recovery route, the view refresher -- are all on the recovery side of that line already.
- Tuning: heartbeat tick 1s, gap threshold 30s (thirty times the tick), connectivity fast-poll 5s, per-connection probe timeout 1.5s.
- The tracker keeps only the most recent sleep interval, and its reading is `was_asleep_since(start)` rather than a general `[start, end]` window. Intervals are recorded in order and cannot overlap, so one that ends before `start` is preceded only by intervals ending earlier still -- which makes every older one unreadable, and the retention sweep that bounded them unnecessary. Narrowing the parameter is what makes that sound: a window ending in the past would need the whole history, nothing asks for one, and offering the parameter would invite a confidently wrong answer.
- Electron's `powerMonitor` is deliberately not used, though `electron/main.js` already subscribes to `resume`/`unlock-screen` for repainting. That signal names the wake but not the window the process missed, does not exist in browser mode or for non-sleep suspensions (SIGSTOP, a hypervisor pause), and would put a laptop-side correctness property behind an IPC hop that can be dropped -- where the heartbeat's own absence is the evidence. Revisit as corroboration if the heartbeat proves noisy, not as a replacement.
- The offline log downgrade applies to both restart steps rather than the start alone: a stop rejected while the device is offline is equally doomed and equally not the machine's fault, and both route through one reporting helper.
- PR #375's dispatch-history machinery has not landed; the owed-set here stays self-contained, and whichever lands second reconciles the two.
- The `subsystems-and-recovery.md` correction is still owed: `gabriel/recovery-audit` has not landed, so the doc carrying the wrong "monotonic advances during sleep" claim does not exist on main yet.

## Open questions

The list below is what was open when the plan was written; every one of them is
answered in the section above, apart from the recorded non-goals.

- Exact SSH quorum endpoints (github/gitlab/bitbucket port 22?) and whether they should be settings-overridable for corporate networks that block some of them.
- Final copy for the two environmental card/band states ("incompatible network" phrasing) — needs a design pass.
- Where the app-level indicator lives (a notice-band variant like BLOCKED, or a providers-panel state).
- The exact mechanism for the dispatch decision awaiting the probe reading (~1-2s) without blocking the stuck-edge callback thread — small worker vs deferred re-dispatch.
- Reconciliation shape with PR #375's dispatch-history machinery, decided by whichever lands second.
- Tuning: heartbeat gap threshold (~30s) and fast-poll cadence (~5s) — pick during implementation, assert in tests.
- Non-goals (recorded, not questions): errored-snapshot attach fallback / events-stream retry pacing (own investigation workspace); Electron powerMonitor corroboration (fallback if the heartbeat proves noisy); wake-triggered invalidation of the forward's cached SSH transports (future wake consumer — needs a minds-to-forward nudge channel); proactive/background connectivity probing; backup-budget re-arm (demoted by the monotonic-freeze finding).

## Findings from the first live log review: two deliberate outages

Evidence: `~/.minds-staging/logs/minds.log` (this branch, pointed at `workspace-1`
on imbue_cloud) against `~/.minds/logs/minds.log` (released 0.4.1, pointed at
geebspace), over 2026-08-20T22:00Z - 2026-08-21T02:10Z. Both apps ran on the same
laptop through the same wifi, the same two deliberate network outages, and the
same 22 sleep intervals, which makes the pair a real A/B for everything except
the machine on the far side.

Two device-level outages appear in the window and are referred to below as the
first (23:19-23:21, every DNS lookup failing with `[Errno 8] nodename nor
servname provided`) and the second (23:55-00:01, DNS and HTTPS working while
every SSH endpoint including the public quorum timed out -- the shape a `pf`
rule blocking outbound SSH produces). Worth flagging rather than assuming past:
Gabriel reports having been connected to wifi throughout. What is measured here
is only *what this device could not reach* in each window -- nothing at all in
the first, nothing over SSH in the second -- not what caused either. If the wifi
association really did hold, the first outage is a device losing all name
resolution while nominally connected, which is a laptop-side condition of
exactly the kind this branch exists to tell apart from a broken machine -- and
the detector called it correctly either way.

### Confirmed working

- **Sleep detection.** 24 heartbeat gaps recorded. 22 carry a frozen monotonic
  clock (e.g. `348s of wall clock ... monotonic advanced 1s, frozen for 347s`),
  confirming the measured premise the whole design rests on. The other 2 (200s
  and 41s) advanced both clocks; the log shows *zero* process activity inside
  both windows, so they were whole-process stalls and suppressing on them is
  correct. Both are also cases Electron's `powerMonitor` would not have fired
  for, which is direct support for the decision not to depend on it.
- **The offline gate, measured against the unguarded app at the same instant.**
  23:19:05 (first outage): staging began probing 18ms after the STUCK edge, read
  `internet=OFFLINE` 62ms after it,
  recorded the block, and withheld the start. Production, at 23:19:05.545, ran it
  and hit `ERROR: Start step ... failed (reported as a backend outage): ... could
  not reach Imbue Cloud: [Errno 8] nodename nor servname provided`, taking the
  Sentry path with it. Staging's owed start fired at 23:21:35 when the network
  returned and the machine was HEALTHY 19s later. Over the window production
  logged 4 restart failures to staging's 1, and 5 Sentry initialisations to 1.
- **Owed-set drain.** 23:22:46 `Dropping the owed start of ...: it is no longer
  stuck` -- the machine came back with the network, so nothing was dispatched.
- **The SSH facet and its tiebreaker**, through the second outage. 23:55:54 and 23:59:42 declared
  `internet=ONLINE ssh=OFFLINE (SSH_BLOCKED)` on a network passing HTTPS while
  the workspace endpoint (`51.81.185.232:22001`) and all three public hosts timed
  out -- the verdict the plan's endpoint-first design exists to produce. At
  00:01:56 the tiebreaker fired exactly as specified: own endpoint still dead,
  public SSH answering, so the facet returned ONLINE and the withheld start was
  released rather than stranded.
- **Wake invalidation** fired 11 times; no reading was carried across a sleep.
- **No faults from the new code.** Zero tracebacks, zero
  `on-wake callback failed` / `ConnectivityDetector callback failed` /
  `Could not start the connectivity gate` lines.

### Problems found

- **The spurious post-wake recovery card is still there, on ~2 of every 3 wakes.**
  14 of the 22 wakes produced a `HEALTHY -> STUCK` at a strikingly consistent
  22.1-23.5s after the wake, each followed by an unattended start and a
  `restarting -> HEALTHY` 13.8-15.4s later. The failure-run reset is *working* --
  every conviction accumulated its 8.0s entirely after the wake, and
  `Probe-failure run ... restarted` never had cause to fire -- but the machine was
  never down: the imbue_cloud host reports 47.9h of unbroken uptime, so all 14
  starts were no-ops. The cause is the recorded non-goal: the forward's cached SSH
  transport dies across the sleep and takes ~30s to re-establish
  (`WS forward ended (client leg ended first)` at +10s, `Timed out reaching the
  backend at http://localhost:8000`, re-established at +29s), so the interface
  genuinely fails for longer than the 5s threshold -- and for longer than the
  8.0s of probe laps the threshold actually takes to accumulate. The changelog's mechanism
  claim is accurate; the user-visible promise it originally carried alongside
  that mechanism -- "closing the lid mid-check no longer produces a spurious
  recovery card when you open it again" -- is not what the logs show, and the
  entry has since been narrowed to claim only that the sleep itself no longer
  convicts. Not a regression, but the evidence for that is architectural rather than
  a matched pairing: production convicts post-wake too, on unmodified code in a
  process frozen by the same sleeps, and the branch's sleep changes only ever
  *remove* observed seconds from a failure run, so they cannot manufacture a
  conviction the unmodified code would not also reach. The two runs are not
  comparable instant-for-instant and should not be read that way -- they point at
  different backends, staging logged 19 STUCK edges in the window to production's
  8, and each convicts at some wakes the other skips. The wake consumer for the
  forward's cached SSH transports is what would actually close this, and it is
  currently deferred.
- **The on-device exemption still ends in a doomed restart and an error-level
  report.** 23:20:08, inside the first outage: a docker-backed workspace was correctly *not*
  gated (`is_network_dependent_workspace` false; dispatched inline in 2ms) and its
  start failed at 23:21:07 with `Modal provider 'modal' failed to initialize:
  Could not connect to the Modal server`, logged at ERROR. `mngr start`
  initialises every configured provider, so a dead network fails the start of an
  on-device machine too -- and because `read_environment_block` answers NONE for
  on-device machines, the warning downgrade cannot apply and the error-reporting
  path runs. This is the exact outcome the branch prevents for remote machines,
  reached around the exemption. The changelog originally promised that "one of
  these going wrong while your wifi is off still offers the restart that fixes
  it"; that did not hold here -- the restart was offered, ran, and failed on an
  unrelated provider -- and the entry now says the restart is offered without
  claiming it succeeds.
- **SSH probe rounds are slow and serial.** 9.25s from the STUCK edge to the
  SSH_BLOCKED verdict (1 workspace endpoint + 3 public hosts, ~1.5-4.6s each) on a
  5s fast-poll cadence. That is about five times the ~1-2s this plan estimated for
  the wait (the open question the worker design answered), and the gate worker is
  blocked for the whole of it. Nothing on the dispatch path is lost to the delay
  itself, for the reason already recorded -- the machine is stuck and `mngr start`
  is idempotent -- and a round does still fit where it has to, at a fifth of the
  ~46s dark-wake windows this laptop actually produces. What the overrun bears on
  is the cost of the sample: the margin shrinks with every endpoint added, and the
  1.5s budget is per *resolved address* inside `socket.create_connection`, which is
  how a multi-homed public host reaches the 4.6s measured here. Since fixed by
  asking a round's endpoints concurrently, so it costs the slowest one rather
  than their sum; the per-resolved-address budget is untouched, so a multi-homed
  host can still spend more than 1.5s on its own.

### Shipped but unexercised in the first window (no evidence either way)

- `workspace_view_refresh` hold/publish: 0 occurrences. The code was live from
  00:10Z, but no machine recovered *as the network returned* after that point --
  both network outages predate it.
- The failure-run reset itself: 0 occurrences. It needs the lid to close while a
  machine is already failing, which did not happen.
- The provider-error probe trigger and the app-level hub notice: neither logs on
  its success path, and the 23:19 probe was demonstrably triggered by the STUCK
  edge (18ms after it), not by a provider error. The 23:59 cold start happened
  inside the second outage, on an SSH-blocked rather than an offline network,
  where discovery's HTTPS poll succeeds and
  so produces no provider error to trigger on -- the first reading came 34s in,
  off the STUCK edge.
- Frontend rendering (the two card states, band copy, the restart-button swap):
  UI frames do not reach these logs.

## Findings from the second live log review: an overnight on AC, then a commute

Evidence: the same two apps on the same laptop, continuous with the first window,
over 2026-08-21T02:10Z - 16:52Z. Neither app restarted. What makes this window
worth more than the first is that none of it was staged: the laptop sat lid-shut
on AC overnight and was then carried to work in a bag, and every condition in it
is one a user would have hit without knowing minds was watching. macOS's own
record (`pmset -g log`) is the ground truth for the power side, and the branch's
heartbeat agrees with it throughout.

The shape of the window, in local time:

- 22:24 - 07:55, lid shut on AC: macOS's Maintenance Sleep cadence -- roughly 15
  minutes asleep, then a 45-180s DarkWake. 48 heartbeat gaps recorded, most of
  them 900-1020s, every one with a frozen monotonic clock.
- 08:03 - 09:16: a 73-minute DarkWake macOS never came out of (no sleep entry
  between 08:02:59 and the 09:15:56 full wake, still on AC). The process ran
  continuously and the heartbeat recorded nothing, correctly.
- 09:16: unplugged and picked up -- FullWake on HID activity, then Clamshell
  Sleep on battery. The commute.
- 09:18:11 the network was still up; by 09:18:23 it was gone, and every DNS
  lookup failed with `[Errno 8]` until 09:47:20. Five DarkWake bursts fall
  inside that outage.
- 09:47:20 the network returned, still inside a DarkWake burst, about a minute
  before the laptop was plugged in at the office.

### Confirmed working

- **The discovery watchdog re-baseline, 24 to 0.** Production bounced the
  discovery producer (`Bouncing mngr latchkey forward observe via SIGHUP`) 24
  times in the window, every one of them within seconds of a wake the branch
  recorded (23 in the 0.2-4.3s after one, the last within a second of one).
  Staging bounced zero times across the same 48 wakes; its own last bounce was
  at 2026-08-20T16:28Z, before the re-baseline went live. Same laptop, same
  sleeps, same subsystem: one app re-kicking its discovery pipeline at every
  wake and the other not. This is the cleanest pairing either window has
  produced, and the first window had no evidence for the watchdog at all.
- **The offline gate, matched instant for instant.** At the 16:33:44Z wake both
  apps convicted their machine after 6.0s of probe failures, 0.3s apart:
  production at 16:33:49.792 dispatched, hit `ERROR: Start step ... failed
  (reported as a backend outage): ... could not reach Imbue Cloud: [Errno 8]` at
  16:33:51.018, and initialised Sentry 2ms later; staging at 16:33:50.114
  recorded `Environment block ... is now OFFLINE` and `Withholding the
  unattended start ...: this device is OFFLINE. It is owed until connectivity
  returns`. Over the whole 14.7h window staging logged 0 ERROR lines and 0
  Sentry initialisations against production's 6 and 6.
- **The owed set drained on a network nobody restored deliberately.**
  16:47:18.344 `stuck -> HEALTHY (probe succeeded)`, 16:47:20.031 `Dropping the
  owed start ...: it is no longer stuck`. The machine carried STUCK and its
  OFFLINE qualifier for 13m28s and was never restarted; the qualifier cleared
  through the ordinary probe-success path.
- **The held view refresh**, which the first window had no evidence for at all.
  16:47:18.344 `Holding the view refresh ...: this device's network is still
  coming back`, 16:47:25.038 `Publishing the held view refresh ...: the network
  has settled` -- held 6.7s, released ~5s after the block lifted at 16:47:20.029.
  The case is precisely the one the decision was written for: the workspace
  probe succeeded 1.7s *before* the detector's facet flipped, so the refresh was
  raised at the transition instant itself, which is the instant that loses a
  reload its scripts.
- **The provider-error probe trigger**, also unevidenced in the first window.
  The commute's first OFFLINE reading landed at 16:18:28.502Z, 15m22s before any
  machine was convicted. The STUCK edge is the only other trigger and there was
  none, so discovery's own failed poll is what raised it -- about 5s after the
  first failure the log shows, an `_ssl.c:993: The handshake operation timed
  out` at 16:18:23 on the heels of a records pull that had succeeded at 16:18:11.
- **The `powerMonitor` corroboration, now quantified rather than inferred.**
  Electron logged 4 `resume` events in the window against the heartbeat's 48
  wakes. DarkWake does not fire `resume`, and DarkWake is the whole overnight.
- **The internet facet costs nothing when the network is simply gone.** 57 probe
  rounds over the 29-minute outage, each three DNS lookups finishing in about
  2ms in total -- against the 9.25s SSH_BLOCKED round measured in the first
  window. The fast poll's open-ended cost is a property of the SSH facet, not of
  the fast poll. Sleep throttled it besides: 57 rounds where a continuously
  awake laptop would have run about 350.

### Problems found

- **A restart in flight across a sleep is killed by a wall-clock timeout and
  reported as an error.** Production did this twice. Dispatched 09:14:15.685,
  `ERROR: Start step ... timed out after 1260s` at 09:50:16.742 -- 0.7s after a
  wake -- then `restart_failed -> HEALTHY` 3.5s later. Dispatched 12:45:23.117,
  timed out at 13:20:14.746, 0.2s after a wake, HEALTHY 3.7s later. Both fired a
  Sentry report; in both the machine was fine and the network was up. The
  mechanism is `subprocess_utils._is_timeout`, which compares `time.time()`
  against a deadline stamped at launch -- wall clock, so a sleep spends the
  budget while the process that would notice is frozen, and the expiry is
  discovered on the first tick after the wake. Two consecutive ~15-minute
  maintenance sleeps is over 1950s against `HOST_START_TIMEOUT_SECONDS` of
  1260s; the awake time inside those two intervals was ~200s and ~135s. This
  branch does not fix it: `read_environment_block` answers NONE for a device
  whose network was working, so the warning downgrade cannot apply, and nothing
  on the restart path consults the `SleepTracker`. Staging escaped it three
  times (the 09:53:19, 10:50:42 and 13:41:17 dispatches) only because each
  straddled a single ~1000s sleep, with 229s, 266s and 321s of margin. The
  overview's "monotonic deadlines already extend across sleep on their own"
  holds for the deadlines this branch sets and not for this one, which was
  already there. The signal that would settle it is in hand:
  `was_asleep_since(<dispatch time>)` distinguishes a start that was frozen from
  one that hung.
- **The post-wake spurious recovery card, at 20 of 48 wakes.** 21 STUCK edges,
  20 of them 22.1-38.1s after a wake with 8.0s of accumulated probe failures,
  each followed by a no-op unattended start and a return to HEALTHY 12-21s
  later. The rate is lower than the first window's 14 of 22, but the absolute
  count is worse: one unattended overnight produced roughly twenty spurious
  cards and twenty no-op `mngr start`s. The 73-minute unbroken DarkWake in the
  middle of this window isolates the cause about as well as an experiment could
  -- 73 continuous awake minutes, same machine, same network, same app, and
  **zero** convictions. Convictions track wakes, not elapsed time. Production
  convicted 19 times over the same sleeps to staging's 21, which is the closest
  the two runs have come to agreeing and is further evidence the branch neither
  causes this nor cures it. The deferred wake consumer for the forward's cached
  SSH transports is still what would close it. Since the CONNECT_ERROR
  decomposition merged from main, the episode's card reads "Reconnecting to
  <machine>..." and a start that reports it booted nothing renders "Not
  responding" rather than "Restart failed" -- the spurious episode is narrated
  honestly, which lowers what closing it is worth.

### Still unexercised after both windows

- The failure-run reset: 0 occurrences across both windows. It needs the lid to
  close while a machine is already failing, and 48 more sleeps did not produce
  one. The reset logs when it fires, so this is the condition not arising rather
  than the reset misfiring quietly.
- SSH_BLOCKED and its quorum tiebreaker: not reached here. The commute network
  was gone outright, so the SSH facet stayed UNKNOWN for the whole outage, which
  is what the design asks for (`internet=OFFLINE ssh=UNKNOWN`). The office
  network read `internet=ONLINE ssh=ONLINE`. The first window remains the only
  evidence for the quorum.
- Frontend rendering: still nothing. Electron's log carries only `wake-repaint`
  lines; renderer state reaches neither log. The two card states, the band copy,
  the button swap and the hub notice remain covered by unit tests and by hand.
