# Retrospective: the `update-chat-theme-controls` harden worker

A user in the staging docker workspace `criticaltest` asked the chat to add dark
mode and font controls, approved the preview, and said "go ahead" at 02:02 UTC on
2026-09-20. The lead (`Chat-1`, agent `b30d6e66`) launched the background harden
worker `update-chat-theme-controls` (agent `0cb5f68a`) at 02:04 and told the user
"10-20 minutes". The worker reported `done` at 05:12, 3 hours 8 minutes later.
As of 19:12 UTC nothing has been applied: the workspace's `main` is still at the
worker's base commit `7605ae488`, the branch `mngr/update-chat-theme-controls` is
unmerged, and the lead has been idle since 03:30.

This document catalogues the process failures behind those two facts: what the
first investigation (Paseo agent `c3fd9a78`, while the worker was still running)
found, what it got wrong, and what only became visible once the worker finished.
The finding that matters for the `gabriel/critical-app-editing` branch pair
stands: none of this behaviour comes from the branch. The rules the worker and
lead followed are unchanged against `origin/main`.

Evidence: the worker transcript
(`/home/user/.minds/accounts/b04aaff92f884a6495ebdd6df672b91c/projects/-home-user-worktrees-update-chat-theme-controls-9b5a9c8918454a1691d4026ce36d33a8/0cb5f68a-5900-417d-bcf2-bfd2a2b97aff.jsonl`
in the container), the lead transcript beside it under `-home-user-workspace/`,
the OOM shed ledger `data/.state/oom_priority/events/shed.jsonl`, the worker's
captured test outputs under the container's `/tmp/`, and the worker's report at
`data/.tasks/harden/update-chat-theme-controls/reports/report.md`. All times UTC.

## Where 191 minutes went

Tool-call durations from the transcript, 02:04:47 to 05:15:35.

| Category | Calls | Minutes | Share |
|---|---|---|---|
| `sleep N; tail ...` polls of backgrounded runs | 57 | 125.3 | 66% |
| Model turns (thinking, planning, writing) | | 41.8 | 22% |
| Foreground vitest / npm test | 13 | 8.1 | 4% |
| Reading files and captured outputs | 90 | 6.4 | 3% |
| Foreground pytest | 26 | 3.4 | 2% |
| Screenshots, builds, lint, misc | 24 | 5.3 | 3% |
| Edits, git, tk | 30 | 0.4 | under 1% |

Coding ran 02:09 to 02:27. Everything after that is verification:

| Window | What ran | Outcome |
|---|---|---|
| 02:16 to 02:24 | chat e2e `-k appearance`, then one e2e | passed |
| 02:29 to 02:40 | chat vitest, shell lint+vitest, prettier, all concurrently | prettier shed at 02:34, vitest fork worker failed; rerun 699 passed |
| 02:40 to 03:04 | full chat pytest, default config (coverage, e2e) | shed by earlyoom at 03:04 after 23.5 min; nothing learned |
| 03:06 to 03:14 | chat pytest minus the two e2e files | 45 failed of 1717: `/tmp` is noexec |
| 03:16 to 03:26 | 4 files with `--basetemp` | 1 failed of 347, passed alone |
| 03:27 to 03:37 | chat `test_e2e.py` | 2 failed of 21 |
| 03:38 to 03:48 | those 2, then that 1 | passed |
| 03:49 to 04:04 | `test_chat_creation_recovery_e2e.py`, twice | 3 failed in 5m17s, then 3 passed in 34s |
| 04:04 to 04:09 | repo-root suite with coverage | 1 failed of 2746 (mngr rename test), not chased |
| 04:11 to 04:51 | full system-interface pytest suite | static at `F..FE` from 04:21; killed by its own `timeout 2400` at 04:51 |
| 04:52 to 05:04 | SI suite in three batches | 359 + 4 passed; e2e 1 failed of 30, passed alone |
| 05:04 to 05:11 | full chat pytest again, with `--cov-append`, in two batches | 1714 + 24 passed; coverage 92% |
| 05:11 to 05:15 | cleanup, report | `done` |

Roughly 7,000 pytest cases executed (plus two partial runs that were killed) for
a 12-file diff containing no Python production code. The chat's Python suite ran
in full twice plus a shed partial; the system interface's Python suite once plus
a hung partial; the repo-root suite once.

Token cost of the run: 329 assistant messages, 57.0M cache-read input tokens,
1.3M cache-write, 138k output. The verification phases after 02:28 account for
167 of those messages and about 39M context tokens, most of them poll turns that
re-read a 150k+ token context to look at three lines of `tail`.

## Issues in the worker's process

### 1. Sleep polling, and why it is structural, not just habit

57 `sleep N; tail file` calls, requesting 213 minutes of sleep in total and
costing 125 minutes of wall time. None of them was necessary as a wait: every
backgrounded command produced a `<task-notification>` when it finished (34 of
them are in the transcript), and Claude Code says so in every backgrounding
result ("You will be notified when it completes"). The worker never once ended
its turn to be woken by one.

Specific failure modes inside that:

- **Sleeps longer than the tool timeout.** The Bash tool's default timeout is
  120s and nothing in either repo raises it (`BASH_DEFAULT_TIMEOUT_MS` is set
  nowhere). `sleep 240`, `300`, `300`, `600`, `700`, `900`, `900` between 02:38
  and 02:54 were each killed at 120s and moved to the background, so the `tail`
  never ran and the call returned nothing. Seven calls, about 14 minutes, zero
  information.
- **Backgrounded sleeps as timers.** `sleep 1500` (twice, 02:56 and 02:57),
  `sleep 480` (03:06) and `sleep 300` (04:46) were launched with
  `run_in_background`. They did nothing except fire notifications 5 to 25
  minutes later that interrupted whatever the worker was doing then.
- **The guesstimate never fit.** After 02:57 it settled on `sleep 115` (44
  calls), a value chosen to fit under the timeout rather than to match anything
  about the run being waited on. Under memory pressure single `sleep 115` calls
  took 209s, 255s, 522s and 580s to return.

Why a hook that blocks `sleep` will not fix this on its own: the lead's
`create_worker.py await` polls the worker's mngr state every 5s and returns
`_AWAIT_IDLE_RC` (76, "finished or wedged without reporting") after 3
consecutive polls in which the worker is `WAITING` or stopped
(`_worker_is_idle`, `_IDLE_POLLS_BEFORE_GIVING_UP = 3`). mngr's activity
tracking (`claude_background_tasks.sh`) only counts the agent's own turn as
activity; a running Claude background task is invisible to it. So a worker that
ends its turn to wait on a 20-minute pytest run is declared idle within about
15 seconds. `lead-proxy.md` says this out loud: "Your own backgrounded commands
are the one exception. Nothing else holds your turn open there, so a worker that
ends its turn waiting on its own command is declared wedged after three idle
polls -- about fifteen seconds. Wait on those with `sleep 60`, repeated". The
sleep loop is the template's own prescription. A sleep guard has to ship with a
liveness check that treats live background tasks as busy, or workers will be
flagged as wedged the first time they comply.

### 2. Test scope: the whole tree for a frontend-only diff

The diff touches 11 TypeScript, CSS and HTML files plus `test_e2e.py`. The
worker still ran the chat's whole Python suite, the repo-root suite and the
system interface's whole Python suite, because that is what the rules say:

- `type-app.md` defines the creation's own suite as
  `cd system/apps/<package> && uv run pytest`, and `harden-creation.md` part 1
  mandates it. No rule narrows it by what the diff touched.
- `harden-creation.md` part 3 says a non-empty `diff.outside_footprint` in the
  scope file means "run the full suite". The three files under
  `system/libs/workspace_ui/` land there for a critical app even though this
  branch's `harden-contention.md` treats `workspace_ui` as part of a critical
  app's footprint for the merge check. This is the one branch-related gap: an
  omission in the branch's scope rules, not a regression.
- The worker read "the full suite" as the repo-root run plus each app's own
  run, because the root `conftest.py` notes the shell and the chat carry their
  own pytest config. That reading is defensible and cost 53 minutes on the
  system interface's Python suite for a change to a CSS token table, a tooltip
  class and an icon.
- `harden-creation.md` line 41 says "Avoid running full test suites unless
  necessary, such as at the very end", and the worker ran the chat suite at
  02:40 right after coding and again at 05:04 for the coverage number. The
  early run was the one that got shed.

### 3. OOM sheds, and the concurrency that invited them

Three sheds of the worker's children in the ledger: prettier (a `node` process
inside the chat vitest run) at 02:34:32, the full chat pytest at 03:04:12, and
the no-e2e chat pytest at 03:14:36 as it was exiting. Agent shells run at
`oom_score_adj` 900 by design, so test processes go first.

The first shed was self-inflicted: the worker launched the chat vitest, the
shell lint+vitest and prettier at the same time on a Docker VM with 4.9 GB and
1 GB of swap shared by every container, with a second workspace container
(`criticaltest-2`, 1.3 GB) alive since 01:48. Nothing in the guidance tells a
worker to serialize heavy runs. The other two sheds hit runs that were already
unhealthy (see 5).

The worker never ran anything with `--no-cov` until its third pytest attempt,
and the default `pyproject` config carries `--cov` and `--durations`. It
discovered `--no-cov`, `--ignore`, `--basetemp`, `-p no:randomly` and its own
`timeout N` wrapper one failed run at a time.

### 4. The 45 failures were an environment fact, not flakes

The 03:06 run failed 45 tests with
`PermissionError: [Errno 13] Permission denied: '/tmp/pytest-of-root/.../fake-mngr'`:
the chat's tests write a fake `mngr` into pytest's tmp dir and exec it, and
workspace `/tmp` is mounted `noexec`. The worker diagnosed this correctly and
fixed it with `--basetemp=$HOME/pytest-tmp` on the next run, and its report
flags it as a host condition the next person will hit. That is good work. The
cost was that three full-suite attempts (02:40, 03:06, 03:16) were needed to
reach a run whose failures meant anything, because the template's pytest
configuration does not already point `basetemp` somewhere executable.

The first investigation attributed these 45 failures to pytest's `--timeout=10`
firing under swap pressure. That was wrong. Five of the 45 did show 10-second
durations, but they were waiting on a `fake-mngr` that could never start.

### 5. Not reacting to a run that is visibly unhealthy

Two long waits were spent on runs the worker had evidence were broken:

- The 02:40 full chat run. At 02:47 a poll returned `+++ Timeout +++` and
  `F.FFF......` at 4% progress. At 02:56 it saw the same marker again and five
  pytest processes, and responded by arming a 1500s wait. It started
  investigating at 03:04; the shed landed eight seconds later. About 17
  minutes between the first evidence and the reaction.
- The 04:11 system-interface run. Progress output stopped at
  `.................F..FE` (54%) at 04:21 and never moved. The worker polled it
  twelve more times over 30 minutes, checked `ps` once at 04:50 (a Chromium
  had been alive for 23 minutes: an SI e2e test hung past every pytest
  timeout), and only learned at 04:52 that its own `timeout 2400` had killed
  the run with exit 124. It never found out which test hung. It split the suite
  into three batches and reran, which passed. The hang itself, a Playwright
  test that outlived `--timeout=10 --timeout-method=signal`, is unexplained and
  unreported.

"No new output for N minutes" is a stronger signal than "the process is still
alive", and the worker never used it.

### 6. Rerun-to-green without a root cause

After the noexec fix, the failures it chased were: 1 of 347 (a server test that
passed alone in 0.85s), 2 of 21 chat e2e, 1 of 2 on rerun, 3 of 3 in the
creation-recovery e2e (5m17s, all timeouts) then 3 of 3 passing in 34s, and 1
of 30 SI e2e. Every one passed on rerun and the report calls them load-induced.
That is plausible, given the swap-full box, but it is an assumption: no failure
was read past its assertion line, and nothing about the box's state at each
failure was recorded. About 35 minutes went to these reruns.

### 7. A third full chat run for a coverage number

At 05:04, with every chat test already green, the worker reran the entire chat
suite plus both e2e files with `--cov-append` (two batches, 7 minutes) purely to
produce a coverage figure for the 75% gate in `pyproject.toml`, because all its
earlier passing runs had used `--no-cov`. Running with coverage once, in
batches, from the start would have removed one of the three full passes.

### 8. Smaller frictions

- Model turns took 42 minutes in total; one turn at 04:21 took 302s and another
  96s, both while the box was swapping.
- The hook that blocks `| tail` fired once and cost a retry.
- The step-tracker (`tk`) reminders fired on every notification, adding noise
  to each poll turn's context.

## Issues on the lead's side (visible only now)

These are the more consequential failures. The worker eventually produced a
correct, well-tested branch. The lead lost it.

- **The estimate had no basis.** "About 10-20 minutes" at 02:02 and 02:05
  appears nowhere in the template. At 03:28 the lead revised to "20-40 more
  minutes, with low confidence". Actual: 3 hours 8 minutes to `done`.
- **Its `await` was killed three times by Claude Code, not by earlyoom.** The
  backgrounded `create_worker.py await` (02:05, `--timeout 90m`) was stopped at
  02:36 with "Background command ... was stopped because the system is running
  low on memory". The re-armed await (02:37) died the same way at 02:44, and a
  backgrounded `until compgen ... sleep 30` loop at 02:46. This is a fourth
  memory-pressure mechanism, separate from earlyoom and the shed ledger, and it
  targets exactly the thing the lead needs to keep alive.
- **It then slept on the worker.** Four foreground
  `for i in $(seq 1 16); do ...; sleep 30; done` loops (02:47, 02:55, 03:04,
  03:12), each 8 minutes, plus `tmux capture-pane` reads of the worker's pane.
  `lead-proxy.md` forbids exactly this ("A worker's report is never polled.
  Sleeping on a `find` over its reports directory, on `mngr list`, or on
  `tmux capture-pane` against its pane is the same guess in a different
  command").
- **It ended its turn with nothing armed.** After the last loop it gave the
  user a status at 03:28:23, the user asked a question at 03:28:32 (whether the
  worker had noticed the OOM kill promptly), and the lead answered at 03:30:42
  and stopped. No `await`, no loop, nothing. The report landed at 05:12:07 and
  nobody read it. The lead's transcript has no events after 03:30:46; its
  process is alive and idle at the prompt.
- **Consequence for the user.** They said "go ahead" at 02:02 and were promised
  a "recently updated" note with a Roll back button. Seventeen hours later the
  chat is unchanged, the branch sits in the worker's worktree, and the report's
  open item for the lead (the two known gaps it left alone) is unread. From the
  user's seat, the feature silently never shipped.

## Review of the first investigation

Agent `c3fd9a78` measured the worker at 112 minutes (03:56) and again at 04:39,
and answered the branch question correctly with the right rule citations
(`harden-creation.md`, `type-app.md`, main's `type-system-interface.md`, the
`agent_rewrite_bash_command.py` banding, the unchanged worker files against
`origin/main`). Its time-category method is the one used above. What it got
wrong or missed:

- It attributed the 45 failures at 03:06 to `--timeout=10` under swap pressure,
  and its saved memory note repeated that. They were `PermissionError` from the
  noexec `/tmp`; the worker's own report has this right. The note has been
  corrected.
- "Every failure it chased passed on rerun" is only true of the later e2e
  failures. The 45 needed a configuration change.
- It read the lead's transcript (ending 03:30) at 03:53 and reported that the
  lead's watcher had been killed and it "fell back to `sleep 30` loops", but
  did not notice that after 03:30 there was no watcher at all, which is the
  failure that actually cost the user the feature.
- At 04:39 it described the SI run as "at 54% with `F..FE` already showing, so
  another rerun loop is likely". The output had been static at that line for
  18 minutes by then; it was a hang, not slow progress.
- It reported sleep polling at 59% of 112 minutes. The final figure is 66% of
  191 minutes; the second half of the run was almost entirely polling.
- It did not identify the `_worker_is_idle` constraint, so its recommendation
  ("wait on the background-task completion notification instead of sleeping")
  would, as stated, get workers declared wedged.
- Before the user withdrew the request it created a Paseo workspace and
  planning agent (`4a538b46`). That agent is closed and archived and its
  worktree is gone; nothing is left over.

## What has to change, and where

All of it is on `main` in the default-workspace-template; none of it is on this
branch. The specifics are for the planning workspace the user intends to open;
this is the list of constraints that plan has to satisfy.

- **Liveness must see background tasks before sleep is banned.** Either mngr's
  activity signal counts a Claude background task as activity, or
  `_worker_is_idle` checks the agent's background-task directory, or the worker
  is told to hold a lightweight wait that the harness itself bounds. A
  `PreToolUse` guard on bare `sleep N` in tool calls (scripts the agent writes
  may still sleep) is only safe after that.
- **Diff-driven suite selection for critical apps.** Frontend suites and the
  app's `test_e2e.py` always; the app's Python suite only when
  `imbue/<app>/` production code changed; and `workspace_ui` counted as inside
  a critical app's footprint so a shared CSS edit does not trip the
  full-monorepo clause.
- **One coverage run, not one uncovered run plus one covered rerun.** If the
  gate needs a number, the first green pass should produce it.
- **Point pytest's `basetemp` somewhere executable in the template config**, so
  the noexec `/tmp` stops costing every worker a full failed run.
- **Serialize heavy runs** and prefer batches under a memory ceiling.
- **Treat "output static for N minutes" as a hang** and diagnose it, instead of
  waiting for a wrapper `timeout` measured in tens of minutes.
- **The lead must re-arm `await` after every interruption**, including the
  Claude Code low-memory kill and any user exchange, and must never end a turn
  with a worker out and no poll armed. The 02:36, 02:44 and 02:46 kills show
  the harness itself can take the poll away; `lead-proxy.md` needs a rule for
  that case rather than assuming the poll survives.
- **Estimates for the user should come from somewhere.** "10-20 minutes" was
  invented; either derive it from the size of the suites the rules will run, or
  do not give one.
