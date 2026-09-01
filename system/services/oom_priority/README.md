# oom_priority

Makes out-of-memory situations in the container degrade gracefully instead of at
the kernel's whim. The actual memory watching and killing is done by
**earlyoom** (a small C daemon, run as a supervised service); this package holds
the small amount of Python that *steers* and *records* it.

## How it fits together

earlyoom picks its victim by reading `/proc/*/oom_score`, the kernel "badness"
value -- which already folds in each process's `oom_score_adj`. So the whole
priority scheme is just: set each process's `oom_score_adj` once, at startup,
into one of a few bands.

- **`bands`** -- the `oom_score_adj` value per band and the helper that writes
  it. From least- to most-expendable: never-kill infrastructure (0) < built-in
  services (`SERVICE_BANDS`, 5-70, ending with the browser coordinator) <
  user-created services (`USER_SERVICE`, 200) < user agent (300) < worker agent
  (600) < agent subprocess (900) < Chromium's own processes (910-1000, renderers
  at the ceiling). Chat agents occupy a *dynamic* range that straddles the worker
  band: `CHAT_AGENT_FLOOR` (300, a chat being engaged with right now) through
  `CHAT_AGENT_BASE` (560, idle but recently used, and the launch band) up to
  `CHAT_AGENT_STALE_CEILING` (800, untouched long enough to count as abandoned).
  The system_interface prioritizer moves a chat within that range from live
  engagement and elapsed idle time (see "Dynamic chat band" below).
  Bands are positive-only: a negative value (true "never kill")
  needs `CAP_SYS_RESOURCE`, which the container does not have, so the never-kill
  infrastructure (sshd, supervisord, earlyoom, tini, tmux) simply keeps the
  inherited default of 0 and is additionally shielded by earlyoom `--avoid`. The
  service order is a best-effort steer, not a hard guarantee -- see "Protection
  is soft" below.
- **`agent_identity`** -- classifies an agent from its label (primary, chat, or
  worker), used by the launch wrapper to pick the band. An agent whose record
  can't be read matches none of these and is tagged least-protected (worker band).
- **`registry`** -- one file per agent recording its main-process pid, so a
  killed pid can be mapped back to "which agent" (earlyoom's after-kill hook is
  handed only a pid that is already gone).
- **`ledger`** -- the append-only shed ledger and the revival-notice bookkeeping.

Tagging happens at three startup points, each setting a process's band directly
without inspecting the process tree:

| What | When | Band | Set by |
|---|---|---|---|
| never-kill infra (sshd, supervisord, earlyoom, tini, tmux) | (inherited) | protected (0) | nothing -- 0 is the default, plus earlyoom `--avoid` |
| a built-in supervisord service | launch | its `SERVICE_BANDS` value | `system/services/oom_priority/bin/oom_tag_service.py <service>` (command prefix) |
| a user-created supervisord service | launch | user service (above every built-in) | `system/services/oom_priority/bin/oom_tag_service.py user` (command prefix) |
| an agent's main process | launch | chat -> the idle-but-fresh chat band (560); worker or unidentifiable -> worker agent | `system/services/oom_priority/bin/agent_oom_launch.py` |
| an agent's subprocesses | each Bash tool call | agent subprocess (most expendable) | `system/scripts/agent_rewrite_bash_command.py` (PreToolUse; also sets the commit identity) |
| the browser coordinator | launch | its `SERVICE_BANDS` value (70, the most expendable built-in service) | `system/services/oom_priority/bin/oom_tag_service.py browser` (command prefix) |
| Chromium's own processes | on fleet events (launch, new page, navigation) | `[SHARED_BROWSER_FLOOR, SHARED_BROWSER]` (910-1000), renderers at the ceiling | the browser service's re-tagging sweep (`browser.oom_retag`) -- see "The Chromium exception" below |

Each supervisord service tags itself the same way an agent's main process does:
its `command` in `system/supervisord.conf` runs `system/services/oom_priority/bin/oom_tag_service.py <key> <the
real command>`, which sets its own `oom_score_adj` from `SERVICE_BANDS` and then
`exec`s the command in place (the band survives `execve` and is inherited by
every child). Built-in services pass their own name; a **user-created** service
(added via the `update-app` skill) passes the `user` key so it is shed before
any built-in service. An unknown key is tagged as `user` too (with a warning):
an unrecognized service must fail *expendable*, never protected.

A **backstop event listener** (`system/services/oom_priority/bin/oom_tag_backstop.py`, the
`oom-tag-backstop` supervisord program) covers the one case the prefix cannot: a
service whose command omits the wrapper entirely, which would otherwise keep the
inherited `oom_score_adj` of 0 and sit as protected as sshd/supervisord. On
every `PROCESS_STATE_RUNNING` event (boot and each restart) it resolves the
program's expected band by *program name* (`bands.supervisord_program_band`: a
built-in's own band; `USER_SERVICE` for anything unrecognized) and raises the
process -- plus any children it already spawned, found via a
`/proc/<pid>/task/*/children` walk -- up to that band. It only ever raises,
never lowers, so a process already tagged higher (a Chromium process the
browser sweep has remapped into its band) and the `PROTECTED` programs
(earlyoom, the listener itself, and the one-shots env-converge and
vm-exec-register) are never demoted. Because this path *raises*, a built-in missing from either
band map is not merely left alone but actively pushed to `USER_SERVICE`, above
every other built-in -- so
`oom_tag_service_test.test_every_built_in_supervisord_program_has_an_explicit_band`
requires every program in `supervisord.conf` to name its band outright, unless
it declares itself user-created by passing the `user` key (for those the
fallback is the intended band, and the two mechanisms agree on it). The
prefix remains the primary mechanism because it tags at spawn:
the RUNNING event fires only after `startsecs` (~1s), leaving a short window
where an unwrapped service runs untagged.

The agent's main process tags *itself*: the `claude` and `worker` agent types'
`command` (in `.mngr/settings.toml`) runs `system/services/oom_priority/bin/agent_oom_launch.py`, which
sets its own `oom_score_adj` to the agent band, records its pid, then `exec`s
claude in place. (Both the `claude` and `worker` types set the command. The
`worker` type has to repeat it rather than inherit it from `claude` because of an
mngr config-load bug: `load_config` ends with a `MngrConfig.model_validate` that
re-marks every agent-type field as explicitly set, so `resolve_agent_type`'s
`parent_type` inheritance treats a child's defaulted `command` as set and clobbers
the parent's value. The config resolver inherits correctly in isolation -- only
the full load path breaks it -- so a worker without this line launches plain
claude and never gets its band. Setting it on both types is the reliable fix.)
Because the band and pid survive `execve`, the tagged process *is* the claude
process, so its band is set before any subprocess exists. A subprocess inherits its
agent's band by default; the PreToolUse hook raises it the rest of the way so a
runaway build/test/browser is always shed first.

## The Chromium exception

Everything above rests on inheritance: tag a process once and its whole subtree
keeps the band. Chromium is the one process in the workspace that breaks this.
Each Chromium process overwrites any inherited `oom_score_adj` once at its own
startup with Chrome's internal gradation (browser/zygote 0, gpu/utility 200,
renderers 300 -- `AdjustLinuxOOMScore` in chromium's `chrome_main_delegate.cc`,
with no flag to disable it). Left alone the memory-heavy renderers would end up
at 300 -- *more* protected than workers (600) and agent subprocesses (900),
inverting the design.

The kernel cannot forbid the lowering: without `CAP_SYS_RESOURCE` any process
may lower its own value back down to its inherited floor (`oom_score_adj_min`,
0 everywhere in this container). But Chromium writes each value exactly once
(its continuous re-adjustment is ChromeOS-only), so an external raise sticks.
The browser service therefore sweeps its descendants and remaps every value
found below `SHARED_BROWSER_FLOOR` (910) into `[SHARED_BROWSER_FLOOR,
SHARED_BROWSER]` via `bands.shared_browser_oom_score_adj`. The mapping is
order-preserving, so Chrome's gradation decides where in the band each process
lands -- which is what makes earlyoom shed one tab's renderer before the whole
browser. The sweep only remaps values below the floor, so it is idempotent.

The input range is Chrome's own gradation (0-300), **not** 0-1000. Scaling
against 1000 would compress every Chromium process into 910-937, the bottom
third of the band, and leave the top to whatever merely *inherited* a high value
-- which is never a renderer, since a renderer always self-writes. That is
exactly backwards: the renderers hold nearly all of a browser's memory and cost
one tab to shed, so they belong at the ceiling.

The same reasoning is why the **coordinator is not in this band at all**. It is
the daemon that launches and drives Chromium, and it is tagged as an ordinary
(if most-expendable) service. Shedding it frees almost nothing: it holds little
memory, the Chromium processes outlive its death, and supervisord restarts it
straight back into the same session. Sitting at the ceiling it was picked first
under every memory-pressure episode while releasing none of the memory that
mattered. A descendant that never self-writes inherits its low service value and
so is remapped near the band's floor, below every renderer -- correct, since it
too holds almost no memory. Crashpad is never remapped at all: it re-parents to
init, so the sweep (which walks the daemon's descendants) never sees it, and it
just keeps the service band it inherited at fork time.

The sweep is event-driven, not periodic: Chromium processes appear only at
moments the fleet observes -- a browser launch, a new page (the CDP observer's
`page` event fires for every new tab, whether opened by an agent command, a
human in the cast viewer, or a page popup), and a navigation (`framenavigated`
fires for every frame, and a cross-site navigation can swap in a fresh
renderer). Each such event triggers a short burst of sweeps (`browser.oom_retag`,
~1s cadence for ~6s), because the processes spawn and self-write their values
over the seconds *after* the event; between events the sweeper sleeps.

## Dynamic chat band

Every agent's band is set once at launch and never changes -- with one exception:
**chat agents**. A chat is a user-facing agent (`user_created` label), and how
expendable it should be depends on how engaged the user is with it, which is only
known at runtime. So the launch wrapper tags a chat at `CHAT_AGENT_BASE` (560),
and the system_interface `ChatOomPrioritizer` moves it in both directions from
there: down toward `CHAT_AGENT_FLOOR` (300) as the user engages with it, and up
toward `CHAT_AGENT_STALE_CEILING` (800) as it is left alone.

Two forces, combined through a single **freshness** factor that decays with idle
time (1.0 for the first hour, reaching 0.0 at 24 hours -- the ramp is a table in
`bands`):

- **engagement** lowers the score: an open tab, a visible tab, and how recently
  the chat was messaged relative to its peers. Each bonus is scaled by freshness.
- **staleness** raises it: `(1 - freshness)` of the distance from
  `CHAT_AGENT_BASE` up to `CHAT_AGENT_STALE_CEILING`.

Because the same factor scales both, engagement *delays* the climb but never
blocks it: a chat with a visible tab that has not been touched in a day still
ends at the ceiling. That is deliberate. Shedding an idle chat costs almost
nothing -- its transcript stays on disk and readable, and the next message
transparently revives it (mngr restarts a `STOPPED`/`DONE` agent before
delivering), so the only cost is a slower next message. Shedding a running worker
destroys in-flight work. So a chat nobody has touched in hours is genuinely worth
less than the worker a live chat just spawned.

The exception is a chat that is **mid-turn** (a running lifecycle state): it is
doing work right now that a shed would destroy rather than defer, so its
staleness climb is suspended for the duration of the turn and it stays below the
worker band. Both edges of the turn count as engagement, so a chat that ran for
three days starts aging from when its turn *ended*, not when it began.

Idle time is measured from the most recent of: a message sent through the UI, the
moment its tab was switched to, either edge of a turn, or -- as a floor -- its own
`claude_process_started` mtime, so a freshly revived chat is never stale. A chat
with no evidence at all counts as fresh: it is demoted only on positive evidence
of abandonment. The lifecycle signal is what covers a chat messaged *outside* the
UI (by `mngr message` or another agent), which the frontend never reports.

Re-tagging is event-driven plus a slow sweep (`SWEEP_INTERVAL_SECONDS`, 60s). The
events -- each `/api/activity` report the frontend posts on tab-presence changes
and after a message send, and each lifecycle change from the observe stream --
cover everything that changes a chat's engagement. The sweep exists solely because
staleness is the one input that changes with nothing to announce it: a chat
crosses a ramp threshold by sitting still. The revive-on-message path is race-free
without the sweep, because the send blocks until the revived process is ready --
the wrapper registers its pid before `exec`, so the pid exists by the time the
frontend reports activity after the send returns.

Across a system-interface restart the per-chat message times are re-seeded from
the durable client-activity log; without that, every chat would look
never-messaged and start aging from its process-start time. An agent whose record
can't be classified as a chat at all still falls through to the worker band, not
a protected one.

## Outputs

- **Shed ledger** (`data/.state/oom_priority/events/shed.jsonl`): append-only,
  written by `system/services/oom_priority/bin/earlyoom_record_shed.py` (earlyoom's `-N` after-kill hook).
  One `process_shed` line per kill, carrying the agent name only when an agent's
  *own* process was shed. Read by the revival-notice hook
  (`system/services/oom_priority/bin/claude_shed_notice_hook.py`) and the launch-task report poll.
- **Agent-pid registry** (`data/.state/oom_priority/agent_pids/<pid>.json`): written
  by the launch wrapper (`system/services/oom_priority/bin/agent_oom_launch.py`), read by the kill hook.

Both live under `runtime/` so they ride the runtime-backup branch. Their absolute
location is pinned via `OOM_PRIORITY_RUNTIME_DIR` (see `.mngr/settings.toml`) so
the container-level kill hook and every agent's per-worktree hooks resolve the
same files. `paths` is the single source of truth for the layout, and -- like
every module here -- is stdlib-only, so the hooks (which run under a plain
`python3`, not `uv`) can import it via a `sys.path` insert.

## Protection is soft

Two things here are best-effort, not hard guarantees:

- **The never-kill infrastructure isn't truly immortal.** Positive-only bands
  plus `--avoid` keep sshd, supervisord, earlyoom, tini, and tmux very unlikely
  to be shed, but under sustained pressure with nothing else to kill earlyoom
  will eventually take one. Hard "never kill" protection (`oom_score_adj -1000`)
  needs `CAP_SYS_RESOURCE`, which the container does not grant -- a deferred
  follow-up.
- **The service ordering can be reordered by memory usage.** earlyoom picks the
  highest `/proc/*/oom_score`, which adds each process's live memory badness on
  top of its `oom_score_adj`. The service bands are only ~10 apart, so a service
  using enough more memory than the one below it can outweigh the band gap and be
  shed first. The bands guarantee the ordering only when memory usage is
  comparable; in the common case the services are lightweight and the order
  holds. Widening the gaps would need to push the top service bands past the
  agent bands, which would defeat the "services outlive agents" goal, so the
  bands stay a steer rather than a strict priority.
