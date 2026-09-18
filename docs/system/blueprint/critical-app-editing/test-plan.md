# Test plan: exercising the critical-app-editing branch end to end

Companion to `plan-critical-app-editing.md`, whose "Manual scenario" section records
that the scenario was never run. This plan covers everything on the branch that can be
tested without an agent following the skill prose: every subcommand and flag of the
scripts, every app change, the manifest library, the memory bands, and the vendored
mngr read side. The skills' prose (which step an agent takes when) is out of scope; the
commands the prose names are in scope, and the end-to-end scenarios run them in the
order the prose prescribes.

Each scenario lists its steps and its pass criteria: things that are true if and only if
the feature worked. Record findings under a `## Findings` heading at the end of this
file, one entry per scenario id, so the plan doubles as the run's record.

## 0. Environment and preconditions

Run in a real workspace on this branch: the flows need supervisord, the app registry,
tmux, `mngr`, and a connected browser client. Two workspaces are ideal (one to break
things in), but one suffices if the emergency scenarios (E15) are run last.

- **S0.1 Branch and vendored mngr.** `git -C /home/user/workspace log -1 --oneline` is on
  this branch. The `mngr` that supervisord runs (`supervisorctl status agent-observer`,
  then `cat /proc/<pid>/cmdline` and `readlink /proc/<pid>/exe`) must be a build carrying
  `ObserveEventFollower` with `require_writer`, or the chat's follower and the observer's
  writer disagree on the file. Check with
  `python3 -c "from imbue.mngr.api.observe import ObserveEventFollower; import inspect; print('require_writer' in inspect.signature(ObserveEventFollower).parameters)"`
  under both the chat's tool env and the env the observer runs in (there are two tool
  envs in a workspace; the one under `/root` and the one under `/home/user` can differ).
- **S0.2 Baseline health.** `supervisorctl status` shows `agent-observer`, `chat`,
  `system_interface`, `terminal` RUNNING. `curl -s localhost:8010/api/health` answers
  `agent_events.is_stream_healthy: true`. `curl -s localhost:8010/_instances` answers 200
  JSON. `python3 system/scripts/layout.py context` shows at least one connected client.
- **S0.3 Identity.** Note `$MINDS_CHAT_ID` (or `$MNGR_AGENT_ID`) of the driving chat; the
  chat previews open on it. Note the shell's, chat's, and terminal's supervisord pids.
- **S0.4 Clean state.** No `data/.state/update-apply/last-good.json`, no `marker.json`,
  no `emergency.json`, nothing under `data/.state/isolated-instances/`. `git status`
  clean. No `editing critical apps` lease in `tk ready`.
- **S0.5 A built worktree.** Provision one as the reference says (the slug `tp1`):

  ```bash
  git worktree add -b mngr/update-tp1 data/.tasks/critical-live/update-tp1 HEAD
  (cd data/.tasks/critical-live/update-tp1 && uv sync --all-packages && cd system && npm ci && npm run build)
  ```

  Time this: it is the one up-front cost the reference hides behind exploration.

## 1. Automated suites (run first, every project the branch touches)

| Project | Command | Notes |
|---|---|---|
| `.agents` scripts | `uv run pytest .agents/shared/scripts .agents/skills/update-app/scripts .agents/skills/update-self/scripts .agents/skills/launch-task/scripts` | root pytest recurses into `.agents` |
| app_manifest | `cd system/libs/app_manifest && uv run pytest` | PreviewSpec validation, built-ins' tables |
| manifests | `uv run pytest system/test_app_manifests.py system/test_supervisord_layout.py` | every critical built-in's table validates; the observer drop-in |
| oom_priority | `cd system/services/oom_priority && uv run pytest` | `agent-observer` band ordering |
| chat | `cd system/apps/chat && uv run pytest` | `test_chat_system.py` runs a real `mngr observe`; confirm it did not skip |
| shell backend | `cd system/apps/system_interface && uv run pytest` | routes, notice watch, preview refusals |
| shell e2e (release) | `cd system/apps/system_interface && uv run pytest -m release imbue/system_interface/test_e2e.py -k "rollback_point or preview"` | release tests do not run in CI |
| shell frontend | `cd system && npm test` | UpdateNotice, UpdateNoticeBand, Sidebar, tabMenu, previewShell vitest |
| terminal | `cd system/apps/terminal && uv run pytest` | `--no-register` boot on two custom ports |
| vendored mngr | `cd system/vendor/mngr && uv run pytest libs/mngr/imbue/mngr/api/observe_test.py libs/mngr/imbue/mngr/hosts/host_test.py libs/mngr/imbue/mngr/hosts/test_host.py libs/mngr_notifications/imbue/mngr_notifications/cli_test.py` | follower, `initial_branch`, notify's probe |

Pass: every suite green, and the chat system test and the shell e2e test actually ran
(not skipped). Record counts.

The unit suites already cover the branches a hand run cannot reach safely: a process
that survives SIGKILL, a rollback that dies from an exception mid-run, an unreadable
`mngr ls`. The scenarios below do not re-create those.

## 2. Component checks

### A. The observer program and the chat as its follower

- **A1 Baseline follow.** `curl -s localhost:8010/api/health | jq .agent_events` reports
  healthy with the "Following the agent-lifecycle event stream" detail. Create a chat
  from the shell; it appears in `curl -s localhost:8010/_instances` within a few seconds
  (the follower folded the observer's `AGENT_STATE`).
- **A2 Observer down for a minute.** `supervisorctl stop agent-observer`. Within the
  follower's fallback poll (10 s) `agent_events.is_stream_healthy` is `false` and the
  detail says the observer exited. `/_instances` still answers 200 with the same list
  (last known list is served, not a 503). The shell's chat tabs keep working. Create a
  chat now: it is created (the create path does not need the observer) and the chat's
  list shows it through the create path's own refresh; note whether it does.
- **A3 Observer back.** `supervisorctl start agent-observer`. Health flips healthy on its
  own; the list matches `mngr ls` (the opening full snapshot replaced the folded view,
  including the chat created during the outage).
- **A4 Observer killed, not stopped.** `kill -9 <observer pid>`. supervisord respawns it
  (`startretries` is effectively unbounded); the chat flips degraded then healthy without
  a chat restart. `supervisorctl status chat` shows the same pid throughout.
- **A5 Chat booted before the observer.** `supervisorctl stop agent-observer chat`, then
  `supervisorctl start chat`, wait for its health, then `start agent-observer`. Between the
  two starts the chat's `/_instances` answers 503 (agent list unknown) and health says
  "Waiting for the first full-state snapshot" (never "exited"); after the observer starts
  it answers 200 with the full list. This is the `require_writer=False` start.
- **A6 Work dir fallback.** `supervisorctl tail agent-observer stderr`: no
  "MNGR_AGENT_WORK_DIR names no directory" line in the normal case. Then test the
  fallback: set the program's env to a missing dir (temporary edit of the drop-in, or run
  the drop-in's command by hand with `MNGR_AGENT_WORK_DIR=/nonexistent`) and confirm the
  line appears on stderr and `mngr observe` still starts.
- **A7 notify's probe.** With the observer running, `mngr notify --help` then a real
  `mngr notify` invocation (the notifications plugin) does not start a second observer:
  `pgrep -af "mngr observe"` shows one. Stop the observer, run `mngr notify` again: it
  starts its own and says so. Stop that one and restart the program.
- **A8 Pre-flight boot unchanged.** `CHAT_PORT=18010 chat-app --preflight` from the repo
  root: `/api/health` answers 200 within the apply's budget, starts no follower
  (`agent_events.is_stream_healthy` is `false` with the "not been started" detail), and
  `pgrep -af "mngr observe"` still shows one process. Kill it.
- **A9 Bands.** `cat /proc/<pid>/oom_score_adj` for the observer, the chat, the shell, and
  the terminal: observer strictly between the shell's and the chat's; the terminal lowest.

### B. The shared isolated-instance script, driven raw

Use the terminal app as the guinea pig (it takes two ports and a store copy) and a
stdlib `http.server` for the failure shapes. Run from the repo root.

- **B1 Named ports, copies, and placeholders (bare shape).**

  ```bash
  python3 .agents/shared/scripts/serve_isolated_instance.py up --name tp-term --cwd . \
    --port main --port sidecar --copy store=data/.apps/terminal --health-path / \
    -- uv run terminal-app --no-register --app-url http://127.0.0.1:{port:main} \
       --instances-url http://127.0.0.1:{port:sidecar} --store {copy:store}/instances.json \
       --state-dir {scratch}/state
  ```

  Pass: stdout is the loopback URL; `data/.state/isolated-instances/tp-term/instance.json`
  records `ports.main`, `ports.sidecar`, `copies.store` under `copies/`, `scratch`, one pid,
  no services, and `inner.command` with the placeholders already resolved. The copied
  store is a real copy (`diff -r` against the live store), and the live store's mtime does
  not change when a session is created through the sidecar's `/_instances`.
  `cat /proc/<pid>/oom_score_adj` of the inner process equals the `user` band.
- **B2 Missing copy source.** Same with `--copy store=data/.apps/nonexistent`: stderr
  carries the "is not a directory; the instance gets an empty" note, the instance boots.
- **B3 Bad placeholders.** `--env X={port:nope}` and `--env X={copy:nope}` each fail before
  a spawn with the "names a port/copy this instance was not given" message and exit 1;
  nothing under the state dir is left behind. `--port main --port main` and a `--port-env`
  naming `main` twice are refused at parse time.
- **B4 Preview shape with inner path.** Re-run B1 adding `--service-name tp-term-app
  --preview-service-name tp-term-preview --preview-title "tp" --inner-path /`. Pass:
  stdout is `tp-term-preview`; the registry (`system/scripts/forward_port.py --list` or the
  registry file) has both rows; `instance.json` has two pids, `wrapper_port`, `inner_path`;
  the wrapper page (`curl localhost:<wrapper_port>/`) embeds the inner service name and
  path as JS literals. `python3 system/scripts/layout.py open tp-term-preview` puts the
  tab on the user's screen and the terminal renders inside it.
- **B5 Refresh in place.** Note `pids[0]`. `serve_isolated_instance.py refresh --name
  tp-term`. Pass: `pids[0]` changed, `pids[1]` (wrapper) and `wrapper_port` and both
  registrations unchanged, the same main and sidecar ports serve again, and the log has a
  second boot marker. Reload the tab (`layout.py refresh tp-term-preview`): it shows the
  terminal again without the tab moving.
- **B6 Refresh onto a boot that fails.** Break the copy (e.g. `chmod 000` the copied
  store's parent) and `refresh`: exit 1, stderr quotes only the lines after the last
  boot marker plus the last probe's status, the new pid is recorded in the state file
  (so `down` can still kill it), the wrapper is untouched, and the tab shows an error.
  Restore permissions and `refresh` again: exit 0.
- **B7 Boot failure on `up`.** `up --name tp-bad --cwd . --port-env PORT -- python3 -c
  "import sys; print('boom'); sys.exit(1)"`: exit 1, stderr quotes `boom` under "last N
  line(s) of data/.state/isolated-instances/tp-bad-failed.log", that file exists after the
  call, and the state dir is gone. Then a health-body case: `-- python3 -m http.server
  {port:main}` with `--health-path /missing` and a short wait: the "last probe: HTTP 404"
  line carries the body excerpt.
- **B8 Teardown escalation.** `up --name tp-trap --cwd . --port-env PORT -- bash -c 'trap
  "" TERM; exec python3 -m http.server $PORT'`. `down --name tp-trap`: it takes about 15 s
  (the SIGTERM grace), then succeeds via SIGKILL; the port is free afterwards; the state
  dir is gone. `down` again: "no active instance", exit 0.
- **B9 `down` deregisters both.** `down --name tp-term`: both registry rows gone, both
  pids gone, the state dir (copies and scratch included) gone, the `-preview` tab pruned
  from the layout on its own.
- **B10 `refresh` with nothing up.** Exit 1 with the "run `up` first" message.

### C. Previews per app through `preview_app.py`

All from the repo root with `uv run python3`, `--worktree data/.tasks/critical-live/update-tp1`.

- **C1 Chat preview.** `preview_app.py up --app chat --worktree <wt> --instance-key
  "$MINDS_CHAT_ID"`. Pass: stdout `chat-preview`; the argv the shared script got (visible
  in `instance.json`'s `inner.command`) is `uv run chat-app --secondary --nudge-shell-url
  ""` with `CHAT_PORT`, `CHAT_HOST`, `CHAT_DATA_DIR` in `env_overrides`; `chat-preview.preview.json`
  records the worktree and an empty `with`; `inner_path` is `/<chat id>`.
  `python3 system/scripts/layout.py open chat-preview` shows the driving conversation
  with its real transcript. Then the secondary contract, against the preview's port:
  - the registry's `chat` row still names port 8010 (no registration);
  - `/api/health` is healthy through the same observer (`pgrep -af "mngr observe"` still
    shows one);
  - every live chat is listed in its `/_instances`;
  - the preview's writes land under `copies/data` (stamps, records, settings), and the
    live `data/.apps/chat` mtime is unchanged after a send;
  - an account switch attempted from the preview page is refused with the "cannot change
    account from a preview" message;
  - `/proc/<agent pid>/oom_score_adj` of a running chat's agent is unchanged after the
    preview's sweep interval (the refusing `set_adj`);
  - **a message sent from the preview reaches the real agent** (the transcript in the
    live chat tab shows it). If it raises a permission card, note where it surfaced
    (open question in the plan).
- **C2 Missing key.** `up --app chat` without `--instance-key`: refused before anything
  boots with the "pass --instance-key" message; nothing under the instances dir.
- **C3 Shell preview alone.** `up --app system_interface --worktree <wt>`. Pass: argv is
  `uv run system-interface --preview --state-dir <copies/state>` with `MINDS_APPS_FILE`
  pointing at `system_interface-preview.registry.toml`, which is a copy of the live
  registry with no rows rewritten. `layout.py open system_interface-preview`. In the tab:
  the user's real projects and tabs, every live app's instances beside them. Against the
  preview's port:
  - `GET /api/inventory` carries `is_preview: true`; the page carries the
    `system-interface-preview` meta tag; no staleness banner appears even though the
    worktree differs from the live tree;
  - `POST /api/apps/chat/instances` (create), delete, rename, location, stop, start of an
    instance, and app stop/start each answer 403 with the "This is a preview of a
    proposed change" detail; `POST /api/updates/pending/confirm` and `/rollback` too;
  - `GET /api/nope` answers a JSON 404, not the app shell;
  - in the tab: tab menus lack Rename/Stop/Start/Delete, the rail row menu lacks the
    new-instance and Stop entries, the New Tab tiles are inert, the all-apps picker rows
    are inert, a stopped app's placeholder has no Start button;
  - a layout edit (drag a tab, pin a shortcut) lands in `copies/state`, and the live
    `data/.state/system_interface` is byte-identical to before (`diff -r`).
- **C4 Shell with the chat (a workspace_ui change).** `down --app system_interface`, then
  `up --app system_interface --with chat --instance-key "$MINDS_CHAT_ID"`. Pass: the chat
  booted first (its state dir's mtime precedes the shell's); the shell's registry copy has
  the `chat` row's `url` and `instances_url` pointing at the chat preview's port and its
  `label` equal to the `chat-preview-app` row's label; `system_interface-preview.preview.json`
  records `with: ["chat"]`. In the shell preview tab the chat tabs render from the
  preview chat (open one; the frame's origin is the `chat-preview-app` label). The chat
  preview's nudger is absent (`{shell_url}` resolved to empty because the shell was not up
  yet); creating a chat from the live shell appears in the preview shell within its own
  sweep, not instantly.
- **C5 Chat preview after a shell preview is up.** With C4 up, `down --app chat` then
  `up --app chat --instance-key ...` again: this time `--nudge-shell-url` is the shell
  preview's loopback URL. Note that the shell preview's registry copy still points at the
  old chat port unless the shell is re-upped; record whether this is a gap.
- **C6 Terminal preview.** `up --app terminal --worktree <wt>`. Pass: two ports; argv has
  `--no-register`, the store copy path, and `{scratch}/state`; the live `terminal` registry
  row still names 7681/7682. Open the tab; create a session from the preview's sidecar
  (`curl -X POST localhost:<sidecar>/_instances`): `tmux ls` on the workspace server shows
  a real session; the live terminal's store is unchanged.
- **C7 Terminal as a sibling is refused.** `up --app system_interface --with terminal`:
  refused with the "serves its instances API apart from its page" message before anything
  boots.
- **C8 Another pass's preview.** With C1 up from `update-tp1`, provision a second worktree
  `update-tp2` (branch off HEAD; no build needed for this check) and run `up --app chat
  --worktree data/.tasks/critical-live/update-tp2 --instance-key ...`: refused with the
  "another pass's preview" message naming the first worktree, exit 1, the running preview
  untouched. The same command with `--worktree data/.tasks/critical-live/update-tp1` is a
  re-up (the shared script's stale-clear then boot): exit 0, new pids.
- **C9 Re-up keeps siblings.** With C4 up, `up --app system_interface --worktree <wt>`
  (no `--with`): exit 0, and the shell's `.preview.json` still lists `chat`. Then `down
  --app system_interface`: the chat preview is torn down too, the registry copy and both
  `.preview.json` files are gone, and the registry holds no `*-preview*` rows.
- **C10 Failed sibling stops the boot.** Break the worktree's chat (rename its `static/`
  dir) and run C4's `up`: the chat fails with the log excerpt, the shell never boots,
  exit 1, the shell's `.preview.json` is kept (it names the chat) or absent (nothing
  booted): record which, and that a following `down --app system_interface` leaves
  nothing behind either way. Restore the directory.
- **C11 Refresh per app.** With each of C1, C3, C6 up: edit a visible string in the
  worktree, rebuild (`cd <wt>/system && npm run build` for the two bundles), `preview_app.py
  refresh --app <name>`, `layout.py refresh <name>-preview`. Pass: the tab shows the change,
  the ports and wrapper are unchanged, and the live app is untouched. For the chat, a
  backend edit (a log line in `server.py`) is picked up by `refresh` alone.
- **C12 Title.** `--title "custom"` shows in the wrapper banner; without it the banner
  reads `<display name> (<worktree basename>)`.
- **C13 Bad worktree.** `--worktree /tmp`: "is not a directory; is --worktree a workspace
  checkout?"; `--app nope`: "no app named 'nope'".

### D. The worker on the pass branch

- **D1 Branch held by the worktree.** With `update-tp1` still checked out as a worktree,
  `create_worker.py launch --name update-tp1 --template worker --runtime-dir
  data/.tasks/harden/update-tp1/ --task-file data/.tasks/harden/update-tp1/task.md
  --branch mngr/update-tp1` (a task file with `operation: update` / `type: app`): mngr
  refuses the checkout of a branch already checked out elsewhere, the refusal reaches
  stderr, no agent is left behind (`mngr ls`), and the launch's "leftover record" hint
  does not fire.
- **D2 Launch on the branch.** Commit a trivial change in the worktree, then run the
  reference's teardown (`preview_app.py down`, `layout.py close`, `git worktree remove`
  without `--force`) and D1's launch again. Pass: the worker exists; `mngr ls --include
  'name == "update-tp1"' --format json` reports `initial_branch: mngr/update-tp1`; the
  worker's `work_dir` is on that branch (`git -C <work_dir> rev-parse --abbrev-ref HEAD`)
  and carries the trivial commit.
- **D3 `git worktree remove` refuses dirty.** Before D2's remove, leave an uncommitted
  file in the worktree: the remove refuses, the reference's "commit it and retry" case.
- **D4 Destroy reports the right branch.** `create_worker.py destroy --name update-tp1`:
  the outcome line names `mngr/update-tp1` (from `initial_branch`) and its unmerged commit
  count; the branch survives. Re-launch, then `destroy --delete-branches`: the branch
  still survives, because mngr did not create it.
- **D5 `launch-sync` reads the branch back.** A tiny task whose worker writes its report
  immediately: `launch-sync --name update-tp1-sync --branch mngr/update-tp1 ...` prints
  a result JSON whose `branch` is `mngr/update-tp1` (not `mngr/update-tp1-sync`).
- **D6 Missing work_dir recovery hint.** On a throwaway agent created with `--branch
  mngr/update-tp1`, `mngr stop`, `rm -rf` its work_dir, `mngr start`: the error's
  `git worktree add` hint names `mngr/update-tp1`.

### E. The apply, the kept rollback point, the notice, and the rollback

Every apply here goes through the reference's step 4 command. Before each, record the
shell's, chat's, and terminal's supervisord pids and `git rev-parse HEAD`.

- **E1 Freshness check.** With `mngr/update-tp1` carrying a chat-only change, the
  reference's `git diff --name-only $BASE HEAD -- ...` is empty. Commit an unrelated
  edit under `system/apps/chat/` on the served branch (then revert it): the diff names
  it. The check is a command, not a script, so this is the whole test.
- **E2 Chat-only apply keeps its point.** Apply with `--keep-rollback-point` and both
  `--worker-bundle` args pointing at the worktree (stand-in for the worker's work_dir).
  Pass: exit 0; `last-good.json` has `apps: ["chat"]`, `programs: ["chat"]`,
  `needs_services_restart: false`, `snapshots` non-empty and every `copy` path present on
  disk; `marker.json` gone; the chat's pid changed and the shell's did not (only the
  services agent restart is expected to turn both over: record which actually happened,
  since the apply restarts the services agent). Every chat tab carries the band with the
  app text; the shell has no top banner; the terminal's tabs have none. Open a second
  browser window: it has the band too. `GET /api/updates/pending` on the live shell
  matches the file.
- **E3 Everything seems good.** Click it in one window: `POST .../confirm` answers 204;
  the band disappears in every window without a reload (socket); `last-good.json` and the
  snapshots dir are gone. Click it again in a window that missed the update: 409 "no
  update notice to confirm", shown as the band's error text.
- **E4 Roll back the chat.** Re-apply a chat change as in E2. Click Roll back: the dialog
  names the applied time, "Chat", and "chat will restart". Confirm. Pass: the route
  answers 202; the band shows "Rolling back: Reverting the update..." then the later
  progress strings; both verbs are hidden while it runs; `data/.state/update-apply/rollback-last.log`
  captures the script's stderr; `git log -1` is the "Rolled back on the user's request"
  revert commit and the served chat is the previous build; the chat's pid changed and the
  shell's and terminal's did not; `last-good.json` has `outcome: "Rolled back to the
  previous version."`, `progress: null`, and the snapshots dir is gone; the band shows the
  outcome with a single Close button; Close removes it everywhere. During the run,
  pressing Roll back in another window answers 409 "already running"; after, it answers
  409 "already rolled back".
- **E5 Shell change: banner, rollback restarts the shell.** Apply a New Tab copy change
  with the flag. Pass: `apps` includes `system_interface`, the top banner shows the shell
  text, no tab bands unless the chat bundle also changed (a `workspace_ui`-free edit
  under the shell's frontend should rebuild both bundles at the npm root: record whether
  `apps` lists the chat too, and whether that matches the spec's "program or bundle
  changed"). Roll back: the shell restarts under the browser; the window reconnects and
  the banner shows the outcome (the socket seeds the notice on connect); the chat's pid
  is unchanged.
- **E6 workspace_ui change.** A shared-library edit: `apps` is both, band on chat tabs
  and banner on the shell, `programs` is both; rollback restarts both and nothing else.
- **E7 Terminal change.** Apply a terminal edit: band on terminal tabs; rollback restarts
  the terminal program only; the tmux sessions behind the tabs survive (ttyd restarted,
  tmux did not) and the tabs reconnect.
- **E8 Services-restart case.** A change under `system/scripts/bootstrap*` (a comment):
  `needs_services_restart: true`; the dialog carries the extra details paragraph. Roll
  back: files restored (the comment gone), no `supervisorctl restart` (pids unchanged),
  outcome names `mngr start --restart system-services`, exit 0 in the log.
- **E9 supervisord table change.** A change to a drop-in under `system/supervisord.conf.d/`
  (a harmless env line): rollback runs `supervisorctl reread` and `update` (visible in the
  log or `supervisorctl status`'s uptime) before the restart.
- **E10 A plain apply replaces the point.** With a notice open, apply another change
  without the flag: the old record and its snapshots are gone before the merge, no new
  record is written, the band disappears. Then with the flag: the new record replaces the
  old, its `merge_sha` is the new merge.
- **E11 Apply refused during a rollback.** Start E4's rollback and, while its progress is
  "Restoring", run an apply: exit 1, "a rollback of the last update is running", nothing
  changed (`git log` unchanged, no marker).
- **E12 CLI refusals.** `update_self.py rollback-last` with no record: exit 1. After E4
  settled and before Close: exit 1 "already settled". `confirm-last` with no record: exit
  0 "nothing to confirm". Two `rollback-last` at once: the second exits 1 "another
  rollback or confirm". `confirm-last` during a rollback: exit 1. `rollback-last` on a
  dirty tree: the precondition error, nothing changed. `rollback-last` while an apply's
  marker names a live pid: exit 1 "an apply is running".
- **E13 A partway record.** Hand-edit `last-good.json` to set `progress` with no
  `outcome` and no lock held: `rollback-last` exits 1 with the "stopped partway" message;
  the shell hides both verbs; `confirm-last` from the CLI still closes it.
- **E14 Auto-rollback (exit 2).** A chat change that breaks its boot (a syntax error in
  `main.py`): the apply's chat pre-flight fails, exit 2, no record, no band, the live chat
  untouched and healthy, the revert commit present. Then a change that boots but never
  serves `/_instances` 200: exit 2 after the settled-verdict budget with the failure
  naming the chat's instances API.
- **E15 Rollback that does not restore health (exit 3).** Only in a workspace you can
  afford to break: after an apply of a chat change with the flag, sabotage the kept
  snapshot copy of the chat's tool environment (or the bundle) so the restore yields an
  unbootable chat, then Roll back. Pass: the outcome says "did not come back healthy",
  `emergency.json` exists, the staleness banner takes over, exit 3 in the log. Recover by
  hand (`git revert` of the revert, rebuild) and confirm the banner clears.
- **E16 update-self keeps no notice.** Run the update-self apply (no flag, `--ff-only`)
  on a trivial fast-forward: no `last-good.json`, no band, `run.json` updated as before.
- **E17 Settled verdict.** In E2's apply output, the phase timings show the post-restart
  probe held for at least three consecutive healthy polls; `supervisorctl restart chat`
  during that window (from another shell) resets the streak rather than failing the apply
  (watch the apply not exit 2). Record the timing.
- **E18 Second pass with a notice open.** With E2's notice open, boot a chat preview
  from a new worktree: `GET /api/updates/pending` names the chat (what the reference tells
  the lead to read); the preview boots regardless; the later apply replaces the point
  (E10 covers the mechanics).

### F. Cross-cutting

- **F1 Preview processes outlive agents.** With a preview up, `oom_score_adj` of its
  inner and wrapper processes equals the `user` band, lower than any agent's.
- **F2 `layout.py open` without a client.** Disconnect every browser and run `open
  chat-preview`: HTTP 412 "has no client to apply it", the reference's screenshot
  fallback trigger. Reconnect: the same open lands.
- **F3 Kept snapshots' size.** `du -sh data/.state/update-apply/snapshots` after E2:
  record it (the open question about never-closed notices).
- **F4 Registry readers under `MINDS_APPS_FILE`.** With C4 up, run `MINDS_APPS_FILE=<the
  shell preview's registry copy> python3 system/scripts/layout.py list`: the chat's URL
  reported is the preview's. `forward_port.py --list` the same way.
- **F5 Scaffold.** Scaffold a new Flask app with `build-app`'s script: its `app.toml`
  carries the `[preview]` table with the package's env names; `load_manifest` accepts it;
  `preview_app.py up --app <new>` boots it over a copy of `data/.apps/<new>` with no
  further configuration.
- **F6 Manifest validation.** Against a scratch `app.toml`: an undeclared `{port:x}`, an
  unknown `{nope}`, `open_path = "/{key}"` without `open_path_takes_key`, an absolute
  `copies` path, and a `health_path` without a leading slash each fail `load_manifest`
  with the named message.

## 3. End-to-end scenarios (the reference's flow, run by hand)

These compose the component checks in the order `references/critical-app.md` prescribes.
Run them after sections A to F pass, in a clean workspace state (S0.4).

- **X1 Chat change, full loop.** Lease (`tk create "editing critical apps"`), worktree,
  composer edit, C1 boot, open, C11 refresh round, commit, D2's teardown and worker launch
  on the branch, worker `done` (or a stand-in that commits a test and reports), optional
  final preview from the worker's `work_dir`, E1 freshness, E2 apply with the worker's
  bundles, band appears, E4 rollback, re-apply, E3 confirm, teardown (`preview_app.py
  down`, `layout.py close`, `destroy`, `tk close`). Pass: every step's criteria above,
  plus: no `*-preview*` registry rows, no worktree, no worker, no lease, no record at the
  end.
- **X2 Shell change.** The New Tab page: C3, C11, harden, E5, confirm.
- **X3 workspace_ui change.** C4 preview, E6 apply, rollback, confirm.
- **X4 Terminal change.** C6, E7.
- **X5 Abandonment.** Start X1, stop after the first preview, and run the step 4 teardown
  as if abandoning: everything released, and a fresh pass on the same slug hits the
  "branch already exists" case the reference describes (`git worktree add -b` fails;
  resume with `git worktree add <dir> mngr/update-tp1` works).
- **X6 Stale lease from another chat.** From a second chat, `tk ready` shows the lease;
  breaking it and running the teardown for the orphaned pass leaves nothing behind.

## 4. Coverage matrix

| Surface | Scenarios |
|---|---|
| `serve_isolated_instance.py up` (ports, copies, placeholders, env, health, service, preview, inner path, failures, band) | B1 B2 B3 B4 B7 F1 |
| `serve_isolated_instance.py refresh` | B5 B6 B10 C11 |
| `serve_isolated_instance.py down` (escalation, deregistration, idempotence) | B8 B9 |
| `preview_wrapper_server.py --inner-path` | B4 C1 |
| `preview_app.py up` (manifest, guard, `--with`, registry copy, `{shell_url}`, key, title, records) | C1 C2 C3 C4 C5 C7 C8 C9 C10 C12 C13 |
| `preview_app.py refresh` / `down` | C9 C11 |
| shell `--preview` backend | C3 |
| shell preview frontend (hidden verbs) | C3 |
| chat `--secondary`, `--nudge-shell-url`, `CHAT_DATA_DIR` | C1 C4 C5 |
| terminal `--no-register` | B1 C6 |
| `agent-observer` program, follower, `agent_events` health | A1 to A6 A9 |
| `mngr notify` probe, pre-flight | A7 A8 |
| `create_worker.py --branch`, `read_worker_branch`, destroy, launch-sync | D1 to D6 |
| mngr `initial_branch`, `checked_out_branch_name`, start hint | D2 D6 |
| `apply --keep-rollback-point`, touched apps, services-restart classification | E2 E5 E6 E7 E8 E10 E16 |
| `rollback-last` (all paths) | E4 E5 E8 E9 E11 E12 E13 E15 |
| `confirm-last` | E3 E12 E13 |
| `wait_settled` | E14 E17 |
| shell notice routes, watch, socket seed, band, banner, dialog | E2 to E5 E13 |
| bands | A9 F1 |
| manifest `PreviewSpec`, scaffold | F5 F6 |
| the flow composed | X1 to X6 |

## Findings

(none recorded yet)
