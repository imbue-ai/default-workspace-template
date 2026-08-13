# Live-testing findings: system-interface live editing

A running list of rough edges and defects found while live-testing the
`submit/system-interface-live-editing-plan` branch (default-workspace-template)
against the `gabriel/denim-pigeon` branch (mngr). Nothing here is fixed yet --
this is the record, not the work.

Each entry records what was observed, where it came from, and how sure we are it
is real. "Repro" means it was reproduced deliberately during this testing pass;
"observed" means it showed up in the sysedit transcript and has not yet been
re-run.

## Status key

- **CONFIRMED** -- reproduced deliberately, cause understood.
- **OBSERVED** -- seen once in the sysedit transcript, not yet re-run.
- **OPEN QUESTION** -- suspected from reading the code; not yet exercised.

---

## From the sysedit transcript (container baked at DWT `4a632acd`)

Session `36267ba1-b77e-4e97-8cf2-9f0e89dddf68`, 2026-08-12 23:14 -> 00:56 UTC:
one complete `update-system-interface` pass adding draw-to-write handwriting
input to the chat composer.

### 1. `create_worker.py await` documented without its required `--name`

**OBSERVED.** At 00:26:24 the lead ran

```
create_worker.py await --task-file data/.tasks/harden/update-draw-input/task.md --timeout 90m
```

which argparse rejects (`--name` is required), exiting 2. Two things made this
worse than a typo:

- The background runner reported the failed command as **"completed (exit code
  0)"**, and the lead's first reaction was "The worker finished. Reading its
  report." It only caught the mistake because no report file existed.
- The command came from the docs. `update-creation/SKILL.md:159` -- the file the
  lead read at 00:24:46 -- spells it `create_worker.py await --task-file ...
  --timeout 90m`, with no `--name`. `heal-creation/SKILL.md:126` carries the same
  abbreviated form. `lead-proxy.md:23` has it right.

This branch already edits `update-creation/SKILL.md`, so the fix is in scope.

### 2. The `for L in desktop mobile` layout loop exits non-zero on a normal workspace

**OBSERVED.** At 23:26:08:

```
opened service:si-preview in tabs=[service:si-preview*]
error: layout op 'open' has no client to apply it (HTTP 412): No connected
client has layout 'mobile' active.
```

The loop exits 1. `update-system-interface/SKILL.md` prescribes this exact loop
at four call sites (lines 170, 256, 327, 429), so on any workspace without a
connected mobile client every `open`/`close` returns failure. A real failure and
an absent mobile client are currently indistinguishable by exit code, which
trains the agent to ignore the loop's status.

### 3. Unexplained console errors on the preview's first load

**OBSERVED.** At 23:27:24, a Playwright load of the preview's inner port reported:

```
Failed to load resource: the server responded with a status of 503 (SERVICE UNAVAILABLE)
Failed to load resource: the server responded with a status of 404 (NOT FOUND)
Failed to load resource: the server responded with a status of 500 (INTERNAL SERVER ERROR)
```

Nobody chased them and the pass succeeded anyway. The 503 is plausibly
`/api/health` polled during boot, and the 404 plausibly a preview-only asset --
but the 500 is unexplained and should be identified before shipping.

---

## Found during manual scripting exercise

Container `minds-dev-gabriel-sysedit`, redeployed to DWT
`submit/system-interface-live-editing-plan` + mngr `gabriel/denim-pigeon` and
committed as `544b7b62` for a clean baseline. Live SI on port 8000 (`OBSERVE`),
preview on 51277 / wrapper 52325 (`FOLLOW`).

### 4. A FOLLOW instance freezes permanently if the live SI restarts under it

**CONFIRMED. Highest severity found.** The boot gate closes the door at boot and
nothing watches it afterwards, so the exact failure the feature exists to prevent
comes back at run time.

Reproduced at the library level first: a live `ObserveEventFollower` records a
failure the moment the observer goes away, and does **not** recover when a new
observer takes the lock 15 seconds later.

```
[t0] writer_running=True  is_alive=True  lines=7
[..] stopping the live system interface (kills the observer)
[t1] writer_running=False is_alive=False
     failure_detail="The 'mngr observe' process writing .../events.jsonl exited,
                     so agent lifecycle events are no longer arriving."
[..] restarting the live system interface (a new observer takes the lock)
[t2] writer_running=True after 15s
[t3] is_alive=False  lines_before_restart=7  lines_now=7
[verdict] follower recovered on its own: False
```

Then confirmed at the application level, with a real second system interface in
FOLLOW mode standing in for the preview:

```
boot:                    200 {"agent_events":{"mode":"FOLLOW","is_alive":true, ...}}
25s after SI restart:    503 {"status":"degraded","is_alive":false,"detail":"...exited..."}
85s after SI restart:    503  (identical -- no self-heal)
same instance, /api/agents: 200, full fresh agent listing
```

That last line is the whole problem. The instance still answers `/api/agents`
with a current listing, so it *looks* fine -- which is precisely the "broken
preview passes its health check" state the strict `/api/health` gate was
introduced to catch. Nothing re-polls `/api/health` after boot, so the user
looking at the `si-preview` tab is never told their agent view has stopped
tracking reality.

The "deliberately no restart" rationale in `_record_events_failure` is
**OBSERVE-specific** -- "the usual cause is that another process legitimately
owns the observe lock, and retrying would just lose it again". A follower takes
no lock, so that reasoning does not transfer: re-probing and re-seeding costs
nothing and is exactly what a follower can safely do.

How likely is this in a real pass? The live loop's own `refresh` targets the
*preview*, not the live SI, so the common path is safe. The exposure is an
unrelated restart during a pass that can run for an hour or more: an OOM shed, a
crash, another flow's `reveal`, or a plain `supervisorctl restart
system_interface`. In this container the freeze reproduced on the first try.

### 5. A failed preview boot never states why, and points at a log it just deleted

**CONFIRMED.** With the live SI stopped (so nothing holds the observe lock),
`preview` correctly refuses -- exit 1, partial instance torn down. But the report
it gives the agent is unusable:

```
up failed: instance did not become healthy on port 49715
  last 40 line(s) of .../si-preview-update-manualtest/instance.log:
  | ... DEBUG | Loading all agents from host host-8d5b...
  | ... DEBUG | Listing agent dir for host host-8d5b...
  | 127.0.0.1 - - [13/Aug/2026 14:28:25] "GET /api/health HTTP/1.1" 503 -
  | (x8 more identical cycles)
```

Two separate problems:

- **The cause is never stated.** All 40 lines are DEBUG discovery chatter and
  bare `503` access-log lines. Meanwhile the health endpoint's *response body*
  carries an exact diagnosis. Verified by booting a FOLLOW instance directly
  against a host dir with no live observer:

  ```
  HTTP=503
  {"status":"degraded","agent_count":3,"agent_events":{"mode":"FOLLOW",
   "is_alive":false,"detail":"No 'mngr observe' process holds
   /tmp/fakehome/.mngr/observe_lock, so there is no live agent-lifecycle
   event stream to follow."}}
  ```

  The health probe records only the status code and throws the body away, so the
  one sentence that would tell the agent what to fix is discarded. This defeats
  the stated purpose of the boot-log-tail change ("the reason for the failure is
  now in front of it").

- **The named log file is gone by the time anyone reads the message.** The
  message says "last 40 line(s) of `<path>/instance.log`", and then
  `tearing down partial instance...` removes the entire instance state directory
  including that log. Confirmed: after the failed boot,
  `data/.state/isolated-instances/` is empty. An agent that tries to read more of
  the file it was just pointed at finds nothing.

### 6. `ObserveEventFollower.is_alive()` returns True for a stopped follower

**CONFIRMED.** Probing the API directly:

```
[follower.stop()] -> None
[after stop] is_alive=True
```

This is documented behavior, not an accident -- the docstring says "False only
once a failure has been recorded. A follower that was never started, or was
deliberately stopped, still reports True". `AgentManager` is safe because it sets
`self._follower = None` in `stop()` and `_render_follow_status` handles the None
case.

The concern is the name, on a newly-public mngr API. `is_alive()` collides
head-on with `threading.Thread.is_alive()`, which means exactly "the thread is
running" -- so an outside consumer that gates on it will report a deliberately
stopped follower as live. `failure_detail() is None` is what the method actually
computes; a name like `has_failed()`/`is_stream_healthy()` would not mislead.

### 7. Unknown `/api/*` paths return 200 + the SPA's index.html

**CONFIRMED, pre-existing (not this branch).** `GET /api/definitely-not-a-route-xyz`
returns HTTP 200 with `<!doctype html>`. Noted because it cost time here (a probe
route appeared to already exist before the refresh that added it) and because it
means any frontend fetch of a mistyped API path silently "succeeds" with HTML.

---

## Verified working (no defect)

These were exercised deliberately and behaved as designed. Recorded so the next
pass does not redo them.

- **`mngr` observe read-side API**, probed directly against the live host dir:
  `is_observe_writer_running` -> True with the observer up, False on a fresh dir,
  and it does not create the lock file; `find_last_full_state_offset` -> a byte
  offset on the live file and `FileNotFoundError` on a missing one; the follower
  refuses a dormant stream with a clear message, and both single-use guards
  (start-twice, start-after-stop) raise. A live follower received
  `AGENTS_FULL_STATE` followed by `AGENT_STATE` deltas within one second --
  exactly the documented seed-then-tail behavior.

- **The follower's actual job -- the thing the sysedit run never tested.** With
  the preview up, a chat agent created afterwards *through the live SI* resolved
  on the preview within 5 seconds (`/api/agents/<new-id>/events` -> 200, not the
  404 that renders as "No conversation data"), the preview's `agent_count` went
  2 -> 3, and after a message was sent through the live SI both instances
  reported the identical transcript (`total=2`). This is the headline fix and it
  works end to end.

- **OOM ownership (`8fc3e63c`, zero prior live coverage).** Chat pid 2887 at
  `oom_score_adj=560`. Posting an open+visible+messaged `/api/activity` snapshot
  to the **preview** returned `{"status":"ok"}` and left it at **560**; posting
  the identical body to the **live SI** moved it to **300**. The FOLLOW instance
  accepts the report and is inert, exactly as designed.

- **`serve_isolated_instance.py refresh --name si-preview-update-<slug>`** (the
  command that replaced the removed `preview-refresh` wrapper): exit 0, inner pid
  1820 -> 7459, **same port 51277**, a backend edit made in the worktree was live
  afterwards, health re-checked green, and the follower re-established in FOLLOW
  mode.

- **`serve_isolated_instance.py down --name ...`**: tears the instance down, and
  a second call is a clean no-op (`nothing to tear down`, exit 0). No orphaned
  inner-server or wrapper processes left behind.

- **The one-preview-at-a-time guard**: booting a second slug while one is up
  exits 1 and names both the conflicting instance and the correct new teardown
  command.

- **The boot hint now names the scripts** (commit `4e9c5470`): the success
  message spells out the full `serve_isolated_instance.py refresh` and `down`
  commands, and says "Opening it puts it on the user's screen."

- **`read_worker_branch`, all three paths**, against real agents:
  `system-services` -> `'mngr/sysedit'` (reads mngr's new `initial_branch`);
  `sysedit` (field is None) -> `WorkerBranchUnknownError: mngr reports no branch
  for agent 'sysedit'; it may not have a git work_dir`; a nonexistent name ->
  `WorkerBranchUnknownError: mngr reports no agent named ...`. The raise is what
  triggers `launch`'s destroy-the-worker path.

- **`initial_branch` matches its documented contract.** Observed on a real host:
  `system-services` reports `mngr/sysedit` while its work_dir has since moved to
  `main`, and chat agents created through the UI report None. Both are exactly
  what `list.md` promises ("Recorded at create time and not re-read... None for
  transfer modes that involve no git"). Prose nit only: the branch changelog's
  opening line says the field "reports the branch an agent's work_dir is actually
  on", which is looser than the field's own (correct) doc.

- **`reveal`, all three outcomes.**
  - *Dirty tree -> exit 1*, nothing changed: "working tree has uncommitted
    changes; refusing to reveal so a rollback can never clobber unrelated work."
  - *Broken backend -> exit 2*, with the failure caught by the pre-flight
    **before the live service was touched**: "merged backend failed to boot in a
    pre-flight check; live service not restarted" -> "rolled back to
    last-known-good; the live UI is confirmed healthy." A revert commit was
    written, the bad import was gone from the served tree, and the live SI stayed
    200 throughout. This path had never been exercised.
  - *Happy path -> exit 0*: freshness check empty, merge, reveal, and the merged
    route served by the live SI with health green.

- **The full Step 3/4 mechanics**: committing the round in the worktree,
  `down` + `git worktree remove` (no `--force`) to free the branch, the
  `git merge-base` freshness check, merge, reveal. All clean.

---

## Residual gaps (not covered by this pass)

- **`mngr notify`'s read-only probe.** The notifications plugin is not installed
  in this container (`mngr notify` -> "No such command"), so the end-to-end
  "Using existing mngr observe process" vs "Starting mngr observe in
  background..." branch was not run. The pieces underneath it are verified:
  `_is_observe_running` imports and is a three-line delegate to
  `is_observe_writer_running`, which is thoroughly exercised above, and the live
  observer demonstrably writes to the path `get_default_events_base_dir` resolves.

- **`create_worker.py launch --branch` end to end.** Not re-run here; it is
  already covered by the sysedit transcript, where launch exited 0 on the current
  code and the worker's commit landed on top of the lead's three.

- **Follower re-seed on truncation / file replacement.** The re-seed path is
  implemented but was not provoked; the observer-restart case (finding 4) is a
  different branch that never reaches it.

- **`reveal` exit 3 (rollback itself fails).** Not provoked.

---

## Open questions from reading the code

### A. What OOM band does the preview process land in?

**OPEN QUESTION.** Upstream `3381143d` bands every supervisord program and
`0985e0c2` fails CI on any service lacking a band. The preview is not a
supervisord program -- `serve_isolated_instance.py` launches it directly -- so
which band its process inherits is unanswered. Worth checking it is not sitting
in the least-expendable band beside the live UI.
