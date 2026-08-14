# Fix plan: system-interface live editing findings

How to resolve everything in `live-testing-findings.md`, plus the defects a
detailed review of both branches surfaced alongside them. Organized as
workstreams in implementation order; each item names the findings it resolves
and the acceptance criteria that prove it.

Repos: "mngr" is the `gabriel/denim-pigeon` branch (PR 341); "DWT" is
`submit/system-interface-live-editing-plan` in default-workspace-template.
Workstream 1 lands first because DWT vendors mngr and the highest-severity
finding (4) is rooted in mngr's follower.

---

## Workstream 1 (mngr): make the follower survive an observer restart

### 1a. Recoverable stream outages (finding 4 -- highest severity)

`ObserveEventFollower._follow_loop` currently breaks out permanently on the
first recorded failure, and `_record_failure` is first-cause-wins forever. The
"deliberately no restart" rationale is OBSERVE-specific (retrying would fight
over the lock); a follower holds no lock, so re-probing costs nothing.

Split failures into two categories:

- **Environmental (recoverable):** no writer holds the lock; the lock probe
  errored. The loop keeps running. `failure_detail()` reports the *current*
  outage while it lasts; when a writer returns, the follower re-seeds and the
  detail clears. Re-seeding is nearly free: a new `mngr observe` writes a fresh
  `AGENTS_FULL_STATE` to the same file, so the follower can simply keep draining
  and let the snapshot replace its folded state -- plus the existing
  truncation/replacement reset in `_drain` for the file-replaced case.
- **Internal (permanent):** the `on_line` sink raised, or the loop itself
  crashed. These keep today's first-cause-wins, dead-forever behavior -- retrying
  a broken consumer just re-breaks it.

Consumer side (DWT `agent_manager.py`): `_build_follow_status` already
delegates to the follower each call, so `/api/health` flips 503 during the
outage and back to 200 on recovery with no consumer change. Keep the strict
boot behavior: `start()` still refuses when no writer holds the lock at boot
(that gate is what stops a broken preview from ever being surfaced), and
`_record_events_failure`'s sticky no-restart semantics stay for OBSERVE mode.

Acceptance:
- mngr unit test reproducing finding 4's library-level script: follower live ->
  observer stopped -> `failure_detail()` non-None -> observer restarted ->
  follower forwards the new snapshot and `failure_detail()` is None again.
- DWT `agent_manager_test`: FOLLOW-mode status goes degraded -> alive across a
  writer restart.
- Live re-run of the finding 4 application-level repro: preview `/api/health`
  returns 503 while the live SI is down and 200 within ~2 poll intervals of it
  coming back, with the new agent listing folded.

### 1b. Rename `is_alive()` (finding 6)

`is_alive()` computes `failure_detail() is None` and collides with
`threading.Thread.is_alive`. With 1a its meaning becomes "the stream is
currently healthy", so rename it `is_stream_healthy()` across mngr, the DWT
consumer, and the `/api/health` body key (`agent_events.is_alive` ->
`agent_events.is_stream_healthy`). Nothing has shipped, so there is no compat
concern; the health body is read only by humans and the probes this plan
teaches to print it (2a).

### 1c. Small review-surfaced correctness fixes (same mngr PR)

- `start()`'s single-use guard checks and sets `_is_started` without the lock;
  take `self._lock` around the guard so two concurrent starts cannot both pass.
- Truncation re-seed: `_drain` resets `_offset = 0` on `size < offset` and
  replays from the file's *first* snapshot; re-run the `_seed` scan instead so
  it resumes from the newest one. (Same-size file replacement -- inode checks --
  is deliberately out of scope; note it in the docstring.)
- `is_observe_writer_running`'s docstring claims its momentary `LOCK_EX` "cannot
  lock out a real observer in any way that matters"; soften to acknowledge the
  microsecond race (an SH lock would block a starting writer's EX just the
  same, so the lock mode is fine -- only the claim overreaches).
- `initial_branch` records the user's `--branch BASE` string verbatim, so a SHA,
  tag, or `origin/main` gets recorded as a "branch" while the work_dir is
  actually detached (or DWIMmed); and an empty `rev-parse --abbrev-ref` falls
  back to recording the guess `"main"`. Fix both by reading the branch back
  from the work_dir after checkout (`git rev-parse --abbrev-ref HEAD`, `HEAD`
  -> None) instead of trusting the input string. This makes the recorded value
  match the field's documented contract by construction.
- `DiscoveredAgent.checked_out_branch_name` returns a stored `""` as-is while
  `read_checked_out_branch` treats `""` as absent; align the property with the
  helper so the offline and online paths agree.
- `mngr_imbue_cloud/hosts/host.py` passes the two branch-name args positionally
  into `super().create_agent_state(...)`; switch to keyword args.
- Changelog prose: the entry's opening sentence says `initial_branch` "reports
  the branch an agent's work_dir is actually on", looser than the field's own
  recorded-at-create-time doc; tighten the sentence.

---

## Workstream 2 (DWT): serve_isolated_instance.py

### 2a. A failed boot states its cause and points at a log that exists (finding 5)

- The health probe (`HttpClient.get_status`) discards the response body, which
  carries the exact diagnosis (`agent_events.detail`). On the *final* failed
  probe of a boot/refresh wait, fetch and print the body (truncated to a few
  hundred bytes) in the failure message, before any teardown.
- The failure message names `<state_dir>/instance.log` and then
  `shutil.rmtree(state_dir)` deletes it. Before the rmtree, copy the log to
  `<state_root>/<name>-failed.log` (one per name, overwritten) and name that
  path in the message instead.

Acceptance: boot a FOLLOW preview with no observer running -> exit 1, the
message contains the health body's "No 'mngr observe' process holds ..." line,
and the named log path exists after the command returns.

### 2b. Boot boundaries in the inner log (finding 15)

`spawn_detached` opens the log `"ab"` and `refresh` reuses it, so a quietly-dead
reboot's excerpt shows the *previous* boot's traceback. Before each spawn, the
parent appends a marker line (`===== boot <name> <iso-timestamp> =====`), and
`_log_excerpt` quotes only lines after the last marker (still capped at 40).
A hung reboot then shows an empty excerpt after the marker -- "the new process
wrote nothing" -- which is the truthful signal, instead of a phantom traceback.

Acceptance: re-run finding 15's two-refresh experiment (crash, then hang); the
hang case's excerpt must not contain the crash case's `ModuleNotFoundError`.

### 2c. `down` verifies death and escalates (finding 14)

`_teardown` sends SIGTERM and never checks. Reuse the machinery `refresh`
already has: SIGTERM -> `_wait_process_gone` -> SIGKILL -> short wait. Only
delete the state dir once every recorded process group is confirmed gone; if
one survives SIGKILL (unkillable D-state), print its pid, keep the state dir,
and exit nonzero -- never report success while leaking a pinned port. `down` on
missing state stays a no-op success; `up`'s partial-instance teardown gets the
same escalation.

Acceptance: re-run finding 14's SIGTERM-trapping service -> `down` exits 0 only
after the process is actually gone (killed by SIGKILL), port unbound.

### 2d. Preview lands in the user-service OOM band (open question A)

The preview inherits its band from the launching shell, and every Claude bash
command self-tags `AGENT_SUBPROCESS` (900) -- so today the preview (and its
wrapper) is the first non-browser thing shed under memory pressure, and with
nothing re-polling health the user's tab just dies silently.

Decision: tag it into the `user` service band (200), the band every
user-created service shares -- the preview *is* a served user-facing app
instance. Implement by prefixing the inner and wrapper spawn commands with the
existing `system/services/oom_priority/bin/oom_tag_service.py user` wrapper,
the same way `build-app` scaffolds services. Lowering 900 -> 200 is permitted
(unprivileged writes are only floor-limited by `oom_score_adj_min`, which is 0
here; the ChatOomPrioritizer already lowers values at runtime).

Tradeoff, accepted deliberately: at 200 the preview outlives every agent,
including the lead driving it. The alternative (leaving it above the agents)
is worse in practice: it kills the surface the user is actively looking at
first, which is the one loss that is invisible until they stare at a dead tab.

Acceptance: after `preview`, `/proc/<inner-pid>/oom_score_adj` and the
wrapper's both read 200.

---

## Workstream 3 (DWT): reveal's dependency path (finding 13)

### 3a. Stop restarting the whole workspace

`_apply_reveal` runs `mngr start --restart system-services`, and the services
agent is the supervisord parent -- so a dependency-only change bounces every
program in the workspace, and the 30s health budget then races a full-stack
restart storm. The dependency refresh (`npm ci`, `uv tool install -e
--reinstall`) only changes the system interface's own venv and node_modules;
nothing else consumes them. Replace the restart with `supervisorctl restart
system_interface` (the same mechanism `update-app` uses for any other service).

Before coding: confirm nothing relied on the services-agent restart (git
archaeology on why `mngr start --restart` was chosen; the plausible answer is
"it predates the supervisord parent/child understanding"). If something does,
document it and fall back to 3b alone with a raised budget.

### 3b. Make the health verdict settled state, both directions

The verdict is a point-in-time probe: run 1 read green in a gap between
restarts, run 2 read red on a change that was fine. Require the verdict to be
*settled*:

- Success requires k consecutive healthy responses (e.g. 3, spaced 1s) AND
  `supervisorctl status system_interface` reporting RUNNING with an unchanged
  pid across the confirmation window.
- Raise the budget from 30s to 60s, matching the shared serve script's boot
  gate (the live gate currently gets half the budget for strictly more work).
- Apply the same settled check to the post-rollback "the live UI is confirmed
  healthy" claim, which finding 13 caught being printed while the pid was
  still turning over.

Acceptance: re-run finding 13's experiment (the cosmetic `keywords` manifest
change, twice, on the same loaded container) -> both runs exit 0 and the live
UI answers 5s and 60s after the success message with the same supervisord pid.

Residual gap to close in the same round: `reveal` exit 3 (rollback itself
fails) has never been provoked; add a unit test that forces
`_recover_running_state` to fail and asserts exit 3.

---

## Workstream 4 (DWT): the layout hand-off (findings 2, 8, 9)

### 4a. Distinguish "no client has that layout" from failure

`layout.py` returns `EXIT_ERROR` (1) for the 412 no-client case, identical to a
real failure, so the prescribed `for L in desktop mobile` loop fails on every
normal workspace and trains leads to ignore it (findings 2, 9). Give the
no-client 412 its own exit code (`EXIT_NO_CLIENT = 4`, message unchanged) --
callers can then branch, and nothing conflates it with a genuine error.

Rewrite the four loop call sites in `update-system-interface/SKILL.md` (and the
matching prose in `update-app`) around delivery rather than per-layout success:

```
APPLIED=0
for L in desktop mobile; do
  system/scripts/layout.py open ... --layout "$L" && APPLIED=1 || {
    [ $? -eq 4 ] || echo "layout $L: real failure" >&2
  }
done
```

with one sentence of prose: exit 4 means no connected client has that layout
active -- harmless as long as *some* layout applied; `APPLIED=0` means the
hand-off did not happen (see 4b).

### 4b. Say what to do when the hand-off cannot be delivered (finding 8)

Add a short prose branch to `update-system-interface` Step 2 (and the final
preview in Step 3): if no layout accepted the `open` (no client connected),
the user is not looking at the preview. Verifying privately and surfacing a
screenshot is the sanctioned fallback, but the lead must say explicitly that
the user has not seen the live surface and that approval on a screenshot is
weaker; re-attempt the `open` when a client connects rather than treating the
screenshot round as the delivered preview.

Acceptance: 4a's snippet exercised against a workspace with only a desktop
client (exit 4 on mobile, APPLIED=1) and with no client (APPLIED=0). The prose
is validated by the next scenario-style live test.

---

## Workstream 5 (DWT): doc-defect fixes (findings 1, 10, 11)

- **Finding 1:** `update-creation/SKILL.md:159` and `heal-creation/SKILL.md:126`
  document `create_worker.py await` without its required `--name`; add it
  (matching `lead-proxy.md:23`). Two of two leads hit this.
- **Finding 10:** add one line to `type-system-interface.md` (the file both
  leads read before driving Playwright): never `wait_until="networkidle"`
  against a system-interface instance -- it holds live connections and never
  settles; use `domcontentloaded`. Two of two leads lost ~40s to this.
- **Finding 11:** the "no `## Change origin` marker" exception is stated only in
  `update-system-interface` Step 3, while `update-creation/SKILL.md` -- where
  the task-file format is defined and where the lead actually looks -- still
  prescribes the marker unconditionally. State the exception at the format's
  definition site: one bullet in `update-creation`'s task-file section noting
  that system-interface harden tasks (per `op-update.md`'s exception) carry no
  marker and no worker gate.

---

## Workstream 6 (DWT): system-interface app

- **Finding 3 (investigate before shipping):** the preview's first load logged
  console errors (503, 500, 404). Repro: boot a preview, drive Playwright with
  console + network capture, and match each error to its request and the
  server's access log. The 503 is a page-issued fetch (nothing in the frontend
  calls `/api/health`), so the candidates are the agent-list/SSE/activity
  fetches racing boot; the 500 must be explained. Fix or explicitly justify
  each before the branch ships.
- **Finding 7 (pre-existing, cheap):** unknown `/api/*` paths return 200 + the
  SPA's index.html, so a mistyped API fetch "succeeds" with HTML. Register a
  JSON 404 for unmatched `/api/*` ahead of the SPA catch-all, with a test.
- **Review-surfaced, tied to 2a's readability:** `_health_endpoint` runs a full
  `_discover_with_filters()` per probe, which is where the 40-line DEBUG wall
  in failed-boot logs comes from (and the boot gate polls it up to 60 times).
  Lower the discovery logging emitted on the health path (or cache discovery
  for ~1s); do not weaken what the endpoint checks.

---

## Workstream 7 (DWT): create_worker.py robustness (review-surfaced)

- `WorkerBranchUnknownError` raised out of `launch_sync` escapes `main` as a
  traceback; catch it and exit 2 like every other failure. Wrap the
  destroy-the-orphan call so a destroy failure cannot mask the original error
  (report both).
- `read_worker_branch` interpolates the worker name into an mngr `--include`
  filter (`f'name == "{name}"'`); validate the name against the same character
  set mngr accepts before interpolating, so a quote cannot silently malform the
  filter (which today surfaces as "no agent named ..." and destroys the worker).

Deliberately not changed: the plain-`launch` path does not read the branch back
(only `launch_sync` does), and `update-system-interface` Step 4 hardcodes
`mngr/update-$SLUG`. That is sound for this flow -- the lead itself passed
`--branch mngr/update-$SLUG` in BASE-only form, so the value is not a guess.

---

## Workstream 8 (mngr, separate PR): the dev-loop `propagate_changes` (finding 12)

Not part of either feature branch, but it degraded scenario 1:

- When Electron ignores SIGTERM for 10s, escalate to SIGKILL on that specific
  pid and start the new instance -- never complete the agent-side update and
  then leave the desktop client down.
- Repeat the failure verdict at the *end* of the (very long) output, not only
  before the rsync wall.
- Fix the debug hint that points at `/tmp/minds-electron.log` when the client
  was never started (say that instead).

---

## Investigations queued behind the fixes

- **`mngr ls` reporting STOPPED for live agents** (scenario 2's caveat): the
  same agents read STOPPED on one query and WAITING minutes later while their
  tmux sessions were plainly alive. If state can read STOPPED for a live
  holder, `worker-failure.md`'s liveness check can bless a lease takeover from
  a live lead. Reproduce under load (the container was swapping); mngr-side.
- **Finding 13 on a quiet host:** after 3a/3b land, re-run the dependency
  reveal on an idle container to confirm the budget is no longer load-sensitive.

## Explicitly deferred

- Frontend re-polling of `/api/health` (a degraded banner in the preview tab):
  with 1a the freeze is transient and self-healing, which removes the
  permanent-silent-staleness harm; a visible degraded state is a nice-to-have.
- Follower detection of same-size file replacement (inode tracking).
- The Step 3 preview-less window between approval and worker completion: by
  design (the branch must be free for the worker's worktree); revisit only if
  it bites in practice.

## Sequencing

1. Workstream 1 on `gabriel/denim-pigeon` (PR 341), since everything else
   consumes it.
2. Re-vendor mngr into DWT, then workstreams 2-7 on
   `submit/system-interface-live-editing-plan`.
3. Workstream 8 as its own small mngr PR.
4. A live re-test round in the sysedit container covering: finding 4's repro,
   finding 13's double-reveal, finding 5/14/15's failure branches, the layout
   loop with and without a connected client, and finding 3's console capture.
