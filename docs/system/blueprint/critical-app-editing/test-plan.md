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
  JSON. `uv run python system/scripts/layout.py context` shows at least one connected client
  (bare `python3` may lack `tomlkit`; every `layout.py` call below runs the same way).
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
  stdout is `tp-term-preview`; the registry file has both rows; `instance.json` has two pids, `wrapper_port`, `inner_path`;
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
  `needs_system_services_restart: false`, `snapshots` non-empty and every `copy` path present on
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
- **E8 Services-restart case.** A change under `system/libs/bootstrap/` (a comment):
  `needs_system_services_restart: true`; the dialog carries the extra details paragraph. Roll
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
- **F4 Registry readers under `MINDS_APPS_FILE`.** With C4 up, read the shell preview's
  registry copy: the chat's row names the preview's URL and label, and the shell preview's
  page frames the chat from that origin.
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

## 5. Second run: scenarios added after the first run

The first run's findings (below) were fixed on the branch and then reviewed; both the
fixes and the review changed behaviour the scenarios above do not name. Run the
scenarios above again (the ones the fixes touched first: B1, C3, C5, C6, C10, E2, E4,
E5, E8, E9, E15, F5, X4), then these. Numbering continues each group's.

### Plan corrections

- **E2's pid criterion is for the rollback, not the apply.** The forward apply restarts
  every critical program through the services agent, so after an apply every critical pid
  has changed whatever the record names. The scoped restart is the rollback's: after Roll
  back, only the recorded `programs`' pids have changed. Read E2, E5, E6, E7 that way.
- **`rollback-last` now also asks whether a page is served.** After the restart it reads
  the shell's health and each restored app's health route for `is_frontend_built`; a
  restored copy that serves no page is an emergency (exit 3, copies kept), not a success.
  E15's bundle variant is therefore expected to fail the rollback, not to pass it.
- **`confirm-last` keeps a touched point's copies.** Close on a settled notice drops the
  record, and the copies only when no rollback ran on the point. E3's "snapshots dir gone"
  holds for an untouched point; after a rollback that failed, the copies must survive Close.
- **D5's `--timeout` is the worker's own completion time.** Pass one the stand-in can meet,
  or accept `timed_out: true` beside the right `branch`.
- **Tool-environment copies need uv-tool launchers.** The staging image's root-venv
  entrypoints are not uv tools, so its snapshots held bundles only (F3, E15). E20 and E24's
  tool-environment variants need an image whose critical apps run from uv tools; record
  which the target is (`uv tool list`, and the snapshot locator's note in the apply output).

### B. The shared script

- **B11 Inner path without a leading slash.** B4's `up` with `--inner-path tp`: refused
  before anything is spawned, exit 1, the message names the path; no state dir, no
  registry rows, no wrapper process. (Before: the wrapper died at once and the health
  wait burned its whole budget on it after the inner server was up and registered.)
- **B12 A page that says it is not built.** `up --name tp-unbuilt --cwd . --port-env PORT
  --health-path /api/health -- python3 <script>` where the script serves 200
  `{"is_frontend_built": false}` on that path: the boot fails within the health budget,
  the "last probe" line quotes the body, the state dir is gone. The same script answering
  `true` boots. This is the probe C10 and C16 rely on.

### C. Previews per app

- **C14 A sibling taken down alone points the frame back at the live app.** With C4 up,
  `down --app chat`: exit 0; the shell preview's registry copy has the `chat` row's `url`
  and `instances_url` on the live chat (8010) again and its label the live label; the
  shell preview tab frames the live chat. Record whether `system_interface-preview.preview.json`
  still lists `chat` under `with`. Then `up --app chat --instance-key ...` again: the copy
  names the new preview port (C5's gap, now expected closed), and `down --app system_interface`
  takes both down.
- **C15 A terminal preview touches nothing of the live workspace.** Note the line count of
  the lead agent's `$MNGR_AGENT_STATE_DIR/events/servers/events.jsonl`. `up --app terminal`,
  then `refresh --app terminal`: the count is unchanged (before: each boot appended a
  `server_registered` event naming a throwaway loopback URL). Create a session through the
  preview's sidecar: the live shell receives no `POST /api/apps/terminal/changed` from it
  (the shell's access log, or the live terminal's tab list in a live window, which must not
  refetch) and the live terminal's `/_instances` is unchanged. Before, the preview's
  nudger posted to the live shell, which refetched the live terminal on the preview's word.
- **C16 An unbuilt sibling fails the boot.** C10 again with the fix in place: hide the
  worktree chat's `static/`, `up --app system_interface --with chat --instance-key ...`: the
  chat fails with the probe's line saying its health reports the page unbuilt, the shell
  never boots, exit 1, and `down --app system_interface` afterwards leaves no record,
  registry row, or state dir. Restore the directory.

### E. The apply, the rollback point, and the rollback

- **E19 Bundle ownership by source stamp.** Both bundles carry `.source-tree-hash`
  (`static/.source-tree-hash` under `system/apps/chat/imbue/chat/` and
  `system/apps/system_interface/imbue/system_interface/`), the hashes of the app's frontend
  tree, `system/libs/workspace_ui`, and the npm lockfile. Three applies with the flag:
  - chat-only frontend edit: `apps: ["chat"]`, `programs: ["chat"]`; the kept shell copy's
    stamp equals the live shell bundle's, the chat's differ; band on chat tabs only.
  - shell-only frontend edit (New Tab copy): `apps: ["system_interface"]`; chat tabs carry
    no band.
  - the chat-only edit again with the worker bundle's `.source-tree-hash` deleted before
    the apply: both apps named, and the apply's stderr carries the "carries no
    .source-tree-hash stamp" note. A build outside a git checkout has no stamp, so this is
    what a bundle from such a build does.
- **E20 A shared backend manifest touches every critical app.** Apply a harmless edit to
  `system/apps/system_interface/pyproject.toml` (a `description` change) with the flag.
  Pass: the plan reinstalls every app's tool environment (`plan.app_tools` names all three;
  the apply output lists three reinstalls); the record's `apps` and `programs` name `chat`,
  `system_interface`, and `terminal`; `snapshots` include a tool-environment copy per app on
  a uv-tool image; band on chat and terminal tabs and the shell's banner. Roll back: all
  three restart, each instances API is held to the settled verdict, every band shows the
  outcome. Before, the record named the shell alone and the rollback left the chat and
  terminal running the updated code over the restored environment.
- **E21 A startup-only change, through the banner.** E8 through the UI: a comment change
  under `system/libs/bootstrap/`, applied with the flag. Pass: `apps: []`, `programs: []`,
  `needs_system_services_restart: true`; the shell's top banner reads "The workspace was
  updated..."; no tab carries a band; the dialog names "the workspace", says "no app
  restarts on its own", and carries the details paragraph. Press Roll back: this rollback
  settles within a second (nothing to restart, no probe), and the route must still answer
  202 and the banner show the outcome naming `mngr start --restart system-services`, with a
  single Close. Before, a script that settled and exited between the shell's two reads was
  answered as a 409 refusal beside an already-settled notice. `rollback-last.log` ends
  with exit 0, pids unchanged, the comment gone.
- **E22 Two windows press Roll back at once.** E4's concurrency again: both presses within
  a second. Pass: exactly one 202; the other is a 409 "already running" shown in its band;
  when the progress arrives over the socket that refusal text disappears from the second
  window's band (before: it stayed beside "Rolling back: ..." and through the outcome);
  both windows show the outcome and one Close. `pgrep -af rollback-last` never shows two;
  `git log` has one revert commit and `git status` is clean afterwards. If a revert fails
  with exit 128 again, `rollback-last.log` now captures git's stderr: record it, the first
  run left this undiagnosed.
- **E23 The shell relays the script's own refusal.** With a notice open, dirty the tree
  (`touch system/scratch.txt && git add system/scratch.txt`) and press Roll back: 409, the
  band shows the script's dirty-tree message verbatim, the notice has no `progress` (the
  script refused before writing any). Undo the staging; Roll back succeeds. Then, with a
  real apply in flight (E11's setup), press Roll back: 409 in the script's "an apply is
  running" words.
- **E24 A rollback that fails keeps its copies, and Close leaves them.** Run last, in a
  workspace you can afford to break. Two variants:
  - E15's bundle variant: empty the kept chat bundle copy, Roll back. Pass: the outcome
    says the previous version did not come back healthy and that the copies are kept,
    `emergency.json` exists, exit 3 in the log, the snapshots dir is still there, the
    staleness banner takes over. From the CLI, `rollback-last` again is refused (exit 1)
    with a reason that says the point was rolled back and the copies kept. Press Close:
    the record is gone, `emergency.json` and the snapshots dir remain. Recover by hand and
    confirm the banner clears; the next apply (any) discards the leftover copies.
  - the revert-conflict variant: after an apply with the flag, commit an edit on the
    served branch to a line the merge changed, then Roll back. Pass: the revert is aborted
    (no revert commit, `git status` clean, no `supervisorctl restart` in the log), the
    outcome names git's conflict and says the copies are kept, snapshots remain, a second
    `rollback-last` is refused, Close leaves the copies, and reverting the extra commit by
    hand then `rollback-last` from a fresh apply works.
- **E25 Nothing to restart, nothing to keep.** Two checks from the first run's warning:
  a drop-in-only apply and rollback (E9) invokes no `supervisorctl restart` (the log has no
  "restart requires a process name") while `reread` and `update` still run; and
  `apply --merge-ref HEAD --keep-rollback-point` (a merge that changes no files) writes no
  record and says so on stderr. Record whether an earlier record's snapshots are still
  discarded first (E10's rule).

### Coverage of the additions

| Surface | Scenarios |
|---|---|
| `serve_isolated_instance.py up` inner-path check, `is_frontend_built` probe | B11 B12 C16 |
| `preview_app.py` reframe on sibling down / re-up | C14 |
| unregistered sidecar: no nudge, no discovery event | C15 |
| bundle stamp comparison, tool-environment touch | E19 E20 |
| workspace-only notice, fast-settling rollback answered as under way | E21 |
| serialized launch, refusal relayed verbatim, refusal scoped to its notice state | E22 E23 |
| restored-page check, copies kept on failure, `confirm-last` on a touched point | E24 |
| empty program list, no-op merge | E25 |

## Findings

### 2026-09-18 — second `criticaltest` acceptance run

**Result: not an unconditional pass.** The first-round fixes and the new rollback/UI
scenarios mostly worked. Two remaining issues and an emergency-banner limitation require attention: terminal previews
still expose their new tmux sessions to the live terminal list (C15), and overlapping
`uv run` commands during a root-venv rollback can interfere with environment restoration
(E20/E11/E12). No production-code fixes were made during this round.

#### Target, method, and automated verification

- Container: `minds-staging-criticaltest`, workspace `/home/user/workspace`.
- Reviewed mngr `198df9eec7` and paired template `aef245dd1ecb` before deploying the
  template source. Staging deployment baseline: `5ee2e333f4aaaddf9fe9e402350a595825289888`.
  Test changes were committed in isolated worktrees and applied through the real script;
  rollbacks used forward commits. Existing staging history was preserved.
- Two independent Playwright clients exercised the real shell, dialogs, sockets, nested
  previews, and reconnects. A private local proxy supplied the workspace hostnames.
  Drivers, command output, screenshots, pytest XML, and `components.jsonl` are retained
  in `data/.tasks/critical-test-round2` and the local round-two evidence archive.
- Core scripts/manifests/instances/OOM: **1188 passed, 1 skipped**.
- Chat, shell backend and browser tests, terminal: **2248 passed, 1 failed, 3 skipped**
  initially. `test_a_tab_whose_instance_goes_unlisted_stays_open_idle_and_reconnects_when_it_is_listed_again`
  timed out opening the fixture's `stub-1` launcher row; its isolated rerun **passed**.
  This is recorded as an intermittent test failure, not erased by the rerun. The real
  chat-observer system test and shell rollback-point browser test ran, rather than skipped.
- Vendored mngr observer/hosts/notifications: **447 passed**.
- Frontend: **1015 passed** (87 + 280 + 648, across 97 files).
- Total: **3884 Python tests and 1015 frontend tests passed**, with four Python skips
  and the one initial failure above. Commands and suite-specific output are in
  `core.log`, `apps.log`, `apps-rerun.log`, `mngr.log`, and `frontend.log`, with XML
  counterparts for pytest. Python suites used explicit paths and separate `/private/tmp`
  basetemps; the apps run included the release browser tests.

#### Remaining findings and expectation corrections

1. **C15 — terminal preview isolation is incomplete, reproduced twice.** Preview boot
   and refresh no longer append server-discovery events; creating a session leaves the
   live terminal store byte-identical and emits no live-shell changed POST. Nevertheless,
   the new `terminal-1` appears in live `GET :7682/_instances`. Both processes enumerate
   the default tmux server, so copying the store does not isolate session discovery.
   Evidence: `C15-detail`, `C15-repeat-result`, and terminal preview logs. Test sessions
   were deleted through the preview sidecar after capture.
2. **E20 with E11/E12 — environment restoration is not protected from CLI startup.**
   A shared manifest edit correctly named and restarted all three critical apps. During
   its rollback, a second `uv run ... apply` and `uv run ... confirm-last` were refused
   as required. The rollback nevertheless logged many `copytree` `FileExistsError`s
   restoring `.venv`, warned that it could not restore that copy, then reported success.
   A later restart failed chat and shell imports (`No module named 'app_instances'`).
   A RECORD inventory found 95 missing files across six packages. Manual package
   reinstalls restored the environment. Concurrent uv synchronization recreating files
   during `rmtree`/`copytree` is a plausible cause; the run does not prove the exact
   interleaving. The root-venv image and deliberately overlapping commands are material
   conditions. Do not generalize this observation to an untested uv-tool image.
   A serial repeat using the existing interpreter, with no overlapping CLI startup,
   restored the copy without warnings and passed imports for all three critical apps
   (`E20-serial-copy-restored`, `E20-serial-imports`).
   Evidence: `E20-rollback-settled`, `E20-recovery-correction`,
   `E20-missing-environment-files.json`, `E20-later-*-startup-errors.log`.
3. **E19 — missing worker stamp safely falls back to a live build.** The unstamped
   worker bundle was rejected with the expected diagnostic, then rebuilt from merged
   source. The resulting stamps allowed `apps: ["chat"]`; the shell was not named.
   The plan's expectation that a missing *worker* stamp necessarily names both apps is
   incorrect for this path. Distinguish it from a missing stamp in the installed/kept
   bundles used to infer ownership. Evidence: `E19-unstamped-result`, `E19-safe-rebuild`.
4. **A5 — a warm stream can serve an already-known list.** Even with every test browser
   disconnected, starting chat before the observer returned 200 with the known list and
   an unhealthy “no observer holds the lock” detail, then recovered when the observer
   started. The 503/waiting criterion requires a genuinely unknown list, not merely a
   stopped writer in this warm workspace. A4's fast supervisor respawn also completed
   between samples; recovery and unchanged chat PID were observed, but a degraded sample
   was not captured.
5. **E24 — emergency staleness banner requires a page reload.** The failed rollback
   outcome appeared in both clients and copies/emergency state survived Close. The
   already-open tabs then showed no warning. Replaying the captured emergency record
   after manual repair and reloading showed the correct staleness banner. The banner
   reads a server-injected meta tag at page load; it is not updated by the notice socket.
   Thus the plan's “banner takes over” expectation is not established for existing tabs
   without reload. Evidence: `E24-after-close-emergency-ui`, `E24-banner-on-reload`.

#### Live scenario ledger

“Partial” identifies a subcriterion or alternate variant not established by this round;
prior-run results and automated coverage are not substituted for a fresh live observation.

| ID | Round-two result |
|---|---|
| S0 | Deployed source, compatible follower, critical services and initial health checked; isolated fixtures prepared. |
| A1 | Healthy follower and real chat creation/listing observed. |
| A2 | 60-second observer outage preserved the list; a later outage allowed creation and immediate listing of a new chat. |
| A3 | Restarted observer recovered without restarting chat; new chat remained listed. |
| A4 | SIGKILL caused observer respawn, unchanged chat PID; degraded interval not sampled. |
| A5 | Warm-stream result differs from 503 expectation; see correction above. |
| A6 | Missing work directory logged the fallback and observer stayed running. |
| A7 | Notify reused the observer, then started its own when the program was stopped; test watcher shut down and program restored. |
| A8 | Preflight health served with follower “not been started”; no extra observer. |
| A9 | Terminal 10, shell 20, observer 24, chat 25. |
| B1 | Real terminal booted on two assigned ports with a copied store and resolved placeholders. |
| B2 | Missing source produced an empty copy and booted. |
| B3 | Unknown port/copy and duplicate declarations refused cleanly. |
| B4 | Wrapper and inner registrations, path, PIDs and browser framing checked. |
| B5 | Refresh replaced inner PID and retained wrapper, ports and registrations. |
| B6 | Failed refresh retained teardown state and wrapper, reported current boot output; valid refresh recovered. |
| B7 | Immediate `boom` exit retained failure log and removed state; HTTP-404 body variant not repeated. |
| B8 | TERM-resistant process required SIGKILL after about 15 seconds; teardown succeeded. |
| B9 | Deregistration, state removal and idempotent teardown passed. |
| B10 | Refresh without an active instance refused. |
| B11 | Invalid inner path refused before spawn and left no state. |
| B12 | HTTP 200 with `is_frontend_built: false` failed with diagnosis; true booted. |
| C1 | Real transcript and preview send/reply worked; live chat store stayed identical. Account-switch and agent-band sweep subchecks not repeated. |
| C2 | Missing instance key refused. |
| C3 | Ten mutation routes returned 403; unknown API returned JSON 404; preview New Tab Chat tile had `aria-disabled=true`. Full menu/drag/stopped-placeholder matrix not repeated. |
| C4 | Composed shell framed chat from the preview origin; registry remap checked. |
| C5 | Bringing chat up again refreshed the live shell preview's registry and nested origin; nudger targeted preview shell. |
| C6 | Terminal preview boot/refresh/create/delete worked; isolation exception is C15. |
| C7 | Unsupported terminal sibling refused. |
| C8 | Another worktree could not hijack the existing preview. |
| C9 | Re-up retained siblings; teardown removed both apps and registrations. |
| C10 | Missing built chat frontend rejected composed preview. |
| C11 | Composer edit visibly rendered; chat/shell/terminal refresh state checked. |
| C12 | Custom preview title visibly rendered. |
| C13 | Invalid app refused; not every argument variant repeated. |
| C14 | Removing only chat retargeted the shell's actual iframe to live chat; re-up restored preview origin. |
| C15 | FAIL: live session list changes despite no live-store write, no discovery append, and no live nudge. |
| C16 | Unbuilt chat prevented shell boot, diagnosed false frontend health, and left clean teardown state. |
| D1 | Checked-out branch refused without leaving an agent. |
| D2 | Real worker launched on the requested existing branch with matching initial branch and reported done. |
| D3 | Dirty worktree removal refused. |
| D4 | Destroy preserved adopted branch, including explicit delete-branches invocation. |
| D5 | Clean synchronous worker completed with done report, correct branch, `timed_out:false`, `destroy_failed:false`. |
| D6 | Missing work directory restart refused with the correct branch hint. |
| E1 | Standalone freshness-race command variant not repeated. |
| E2 | Chat-only kept point, copies, band in two clients, no shell banner. |
| E3 | Confirm returned 204, cleared both clients and untouched copies; repeat returned 409. |
| E4 | Actual UI rollback succeeded, one forward rollback commit and clean tree; only chat restarted. |
| E5 | Shell-only point/banner and scoped rollback passed. |
| E6 | Shared UI point named chat+shell; rollback restarted those two only. |
| E7 | Terminal-only rollback restarted only terminal and preserved exact post-apply tmux sessions. |
| E8 | Startup-only point had no apps/programs and named required services restart in its outcome. |
| E9 | Drop-in rollback used reread/update without an empty restart command. |
| E10 | Later kept point replaced earlier point; plain and no-op applies removed prior copies. |
| E11 | Real rollback lock refused another apply. See E20 environment-race finding. |
| E12 | Confirm refused during rollback; second rollback of settled point refused. |
| E13 | Synthetic partway progress refused rollback and hid action verbs. CLI confirm variant not repeated. |
| E14 | Invalid Python failed preflight, exit 2, forward recovery, no notice and unchanged critical PIDs. Never-healthy instances variant not repeated. |
| E15 | See E24 bundle variant. |
| E16 | Plain fast-forward apply kept no point and wrote UPDATED run status. |
| E17 | Clean repeat succeeded in 26 seconds after a deliberate chat restart reset the settling window; earlier 149-second attempt needed manual repair and is not an independent pass. |
| E18 | Previews coexisted with kept points, but fresh second-pass boot variant not separately repeated. |
| E19 | Chat/shell ownership passed; unstamped worker was safely rebuilt, expectation correction above. |
| E20 | All-app classification/restart passed; overlapping-command environment restore failed, see finding. |
| E21 | Actual dialog wording verified; fast startup-only rollback returned 202, unchanged PIDs, correct outcome. |
| E22 | Exactly one 202 and one 409 from two simultaneous dialogs; refusal cleared as progress arrived; both showed outcome. |
| E23 | Dirty-tree 409 and script's exact explanation appeared in band with no progress; subsequent rollback passed. Real apply-in-flight UI variant not repeated. |
| E24 | Both failure variants retained copies, refused repeats and preserved copies on Close. Conflict aborted cleanly; fresh retry succeeded. Missing bundle returned 3 and kept emergency state; manual restoration and next-apply cleanup passed. Emergency banner required reload; see finding. |
| E25 | Drop-in and no-op cases passed; prior snapshots removed, no empty restart diagnostic. |
| F1 | Inner/wrapper memory scores matched user band. |
| F2 | Zero clients returned HTTP 412; explicit reconnected client opened preview. Two unidentified clients also correctly require `--client`. |
| F3 | Kept frontend bundle snapshots totaled 1020 KiB in the final chat apply; shared manifest also copied the root venv. |
| F4 | Registry and actual nested frame used preview origin. |
| F5 | Scaffolded app imported `os`, validated manifest, booted with copied data and served its page. |
| F6 | All five invalid manifest shapes rejected with named diagnostics. |
| X1 | Chat preview/edit, worker handoff, apply, rollback and confirm exercised with controlled fixture changes. |
| X2 | Shell preview/edit/apply/rollback exercised; semantic hardening by an independent worker not evaluated. |
| X3 | Shared UI composed preview, apply and rollback exercised. |
| X4 | Terminal preview/apply/rollback exercised, with C15 failure. |
| X5 | Abandoned branch could not be recreated with `-b`; resume and clean removal passed. |
| X6 | Cross-chat stale-lease takeover not repeated. |

#### Harness qualifications

- The test browser required `--disable-dev-shm-usage` for this image's 64 MB shared-memory
  mount. Closing all pages also ended the initial driver (it waited on a closed page);
  restarting that private driver restored testing. Neither is an app regression.
- Initial UI assertions ran before socket delivery or on a client without a chat tab;
  repeats used the intended tabs and waited for delivery. “Close” propagated without
  reload. Native `disabled` was the wrong assertion for the preview tile's `aria-disabled`.
- The first D5 attempt inherited the services-agent identity and was interrupted by an
  overlapping services restart. It was replaced by a clean, independently identified run.
- Terminal session preservation must compare immediately after apply with after rollback;
  apply itself recreates the services-agent tmux session. The corrected comparison passed.
- Browser response listeners accumulated in later drivers; duplicate log entries in those
  arrays are duplicate callbacks, not duplicate network requests. E22's original isolated
  two-window assertion observed exactly one 202 and one 409.
- Rollback logs append across invocations. Old first-round exit-128 output and earlier
  E20 restore warnings must not be attributed to later successful rollbacks.
- Active critical launchers in this image use the root venv. Per-app uv-tool snapshot
  variants were unavailable, as the apply's locator diagnostics explicitly report.

#### Final restoration

Restored the deployed source with forward commit
`0d8411769ae35211df9fdd66adbf651c206bb451`; its tracked tree exactly matches deployment
baseline `5ee2e333f4aaaddf9fe9e402350a595825289888`. Rebuilt the frontends and restarted
observer/chat/shell/terminal. Both page-health routes report built frontends, the chat
follower is healthy, chat instances contain only the original Welcome chat, and terminal
instances are empty. An installed-file audit found no missing files after recovery.

All round-two worktrees, previews, workers, test chat/session, and kept/emergency/active
update state were removed; fixture branches and evidence remain for diagnosis. The
original Welcome and system-services tmux sessions remain. The critical-app editing
lease was released. The workspace tree is clean and only its main worktree remains.
Fresh log output contained only expected chat-to-shell connection refusals during the
brief ordered restart, with no new traceback. One-shot env-converge and vm-exec-register
are EXITED as expected; the long-running programs are RUNNING.

The archive excludes the 92 MB pre-deployment source tar (still retained in the
container) and includes the XML, logs, JSON evidence, drivers, and screenshots. Local
artifact: `.test_output/criticaltest-round2-2026-09-18.tar.gz` in the mngr worktree.


### 2026-09-18 — `criticaltest` Docker staging acceptance run

**Result: automated coverage passed after environment corrections; live acceptance
found blockers. This is not a clean acceptance pass. No product fixes were made.**
Test-only edits, deliberate failures, and recovery commits were confined to staging.

#### Target and evidence

- Container: `minds-staging-criticaltest` (`c94b7e1c9a20`), workspace
  `/home/user/workspace`; connected using `docker exec`.
- Reviewed template checkout `7b55117bd`, with vendored mngr `198df9eec7`.
  The container's flattened initial commit was `f362d9ea8`. SHA-256 checks of all
  154 changed files present in the template matched the staging files.
- Evidence: `/home/user/workspace/data/.tasks/critical-test-results/` in the
  container. `components.jsonl` records commands, statuses, pids, and update
  records. The directory also contains JUnit XML, command logs, and screenshots.
- Browser checks used Fortress with a local registry-based HTTP/WebSocket
  forwarding adapter. This tested the real app frontends and APIs, but **not the
  desktop/connector forwarding path**. No browser client was connected initially.
- The hardening worker was the plan's permitted small stand-in, not a full
  hardening/review run. Its branch, commit, report, and destruction were checked.

#### Automated suites

| Selection | Result after targeted reruns |
|---|---|
| Shared scripts, update-app, update-self, launch-task, app_manifest, manifest/layout checks, oom_priority | 986 passed, 1 skipped |
| Chat, system_interface, terminal | 2,244 passed, 3 skipped |
| Vendored mngr observe/host/notification selections | 447 passed |
| Frontend npm workspaces | 87 + 277 + 648 = 1,012 passed |
| Explicit shell release `-k "rollback_point or preview"` | 1 passed, 29 deselected; repeats coverage above |

That is **4,689 passing tests and 4 skips**, excluding the explicit duplicate
release run and repeated tests in the targeted reruns. The chat system test and
shell browser tests ran. The four skips were the deliberately disabled expensive
nested-dispatch test, a file-permissions assertion skipped as root, the
Claude-to-Pi release test needing two API credentials, and the live Claude
message-conservation release test needing its credentials file.

The initial runs were not clean: `/tmp` is mounted `noexec`; a subsequent temp
path inside the checkout made nested git tests find the parent repository, and
long temp paths exceeded the Unix socket limit. There was also one intermittent
already-reaped-child failure. The corrected targeted reruns passed (58 core and
23 app tests). Use **`/private/tmp`**, which is executable, outside the checkout,
short enough for sockets, and accepted by mngr's temporary-HOME guard. No mount
change was made. Vendored mngr required `uv sync --all-packages` first.

Reproduction commands, from the workspace root unless indicated:

```sh
uv run pytest .agents/shared/scripts .agents/skills/update-app/scripts \
  .agents/skills/update-self/scripts .agents/skills/launch-task/scripts \
  system/libs/app_manifest system/test_app_manifests.py \
  system/test_supervisord_layout.py system/services/oom_priority \
  --basetemp=/private/tmp/critical-core
uv run pytest system/apps/chat system/apps/system_interface system/apps/terminal \
  --basetemp=/private/tmp/critical-apps
# From system/vendor/mngr, after uv sync --all-packages:
TMPDIR=/private/tmp uv run pytest libs/mngr/imbue/mngr/api/observe_test.py \
  libs/mngr/imbue/mngr/hosts/host_test.py libs/mngr/imbue/mngr/hosts/test_host.py \
  libs/mngr_notifications/imbue/mngr_notifications/cli_test.py \
  --basetemp=/private/tmp/critical-mngr
# From system:
npm test
```

#### Fixes landed after this run (2026-09-18)

Each numbered finding below maps to a change on the branch, verified by unit and
code-level tests only; the live scenarios were not re-run.

1. An unregistered sidecar boot (`terminal-app --no-register`) is no longer held to
   the manifest's `instances_url`, so the terminal preview boots (B1, C6, X4 owed a re-run).
2. The scaffolded runner imports `os`.
3. `rollback-last` asks the shell's page and each restored app's health route whether
   the restored bundle is served; a copy that restored no page is an emergency (exit 3,
   `emergency.json`) and the copies are kept, which the outcome says.
4. The shell's rollback launch is serialized and answers once the script has written its
   first progress; a second press reads that progress and gets the 409, and a script
   that refuses (dirty tree, apply in flight) is a 409 in its own words. The
   `git commit` exit 128 the first experiment hit is still undiagnosed.
5. A bundle rebuilt from unchanged source no longer makes its owner touched: the record
   compares each bundle's source stamp with the kept copy's, so a chat-only change names
   the chat alone (E2's criterion). A build with no stamp still names both.
6. A notice whose `apps` is empty is carried by the shell's banner, with its own text and
   a dialog naming the workspace.
7. An unknown `/api/...` GET answers a JSON 404.
8. A sibling re-upped or taken down on its own is written into the registry copy of every
   live preview that frames it.
9. The isolated-instance health probe reads `is_frontend_built` from a JSON health answer
   and fails the boot on `false`.
10. No fix: `launch-sync`'s timeout is the worker's own completion time; the branch
    it reported was right.
11. A rollback with no recorded program skips the restart; a merge that changed no files
    keeps no rollback point.

#### Findings requiring attention

1. **B1/C6 — terminal preview cannot boot.** The sidecar manifest declares
   `http://127.0.0.1:7682`, while `--instances-url` uses an allocated preview port.
   `_load_sidecar_manifest` rejects the mismatch even with `--no-register`.
   Terminal preview, its refresh, and the preview portion of X4 are blocked.
   Raw launcher coverage used a stdlib HTTP server instead.
2. **F5 — the scaffolded runner crashes.** The generated runner reads
   `os.environ` without importing `os`; preview exits with `NameError`. Manifest
   generation/validation succeeds. Evidence: `f5-scaffold.log`, `f5-sync.log`,
   and F5 entries in `components.jsonl`.
3. **E15 — rollback can falsely report healthy and discard recovery copies.**
   Emptied the kept `chat_bundle` directory after a shared-UI apply, then rolled
   back. Exit was **0**, outcome was “Rolled back to the previous version,” and
   snapshots were discarded. Chat health was HTTP 200 with
   **`is_frontend_built: false`**; its page said “This workspace's chat interface
   is not built yet.” No `emergency.json` was written. The instances API and
   status-only health checks do not catch this frontend failure. Evidence:
   `rollback-missing-bundle15.log`, `E15-after` in `components.jsonl`.
4. **E4 — two simultaneous browser rollback requests both return 202.** The
   expected second-request 409 did not occur. One detached invocation refused
   the lock; the other failed its revert commit with exit 128 and left staged
   changes. The exact cause of that commit failure remains undiagnosed. The
   test-owned staged revert was committed to recover. Later uncontended browser
   rollback succeeded, including socket reconnection and outcome dismissal.
   Evidence: `e4-concurrency.json`, `E4-failure-state` in `components.jsonl`.
   CLI lock-held tests separately passed; the subsequent apply in the first
   experiment ran after the failed rollback exited, not through a held lock.
5. **E2/E5 — rollback scope is broader than the chat-only expectation.** A
   composer-only edit recorded both `chat` and `system_interface`, took both
   bundle snapshots, and displayed both chat bands and a shell banner. Shell-only
   frontend edits also recorded both. Uncontended rollback restarted both, leaving
   terminal unchanged. Root npm builds/snapshot classification explain the
   observed behavior, but E2's chat-only acceptance criterion is not met.
   Forward apply restarts all critical programs through the services agent.
6. **E8 — a startup-only change has no visible rollback notice.** A comment in
   `system/libs/bootstrap/src/bootstrap/manager.py` correctly set
   `needs_system_services_restart: true`, but `apps` and `programs` were empty. There was
   no app band/banner from which to open the required explanatory dialog. CLI
   rollback restored the source without changing service pids and correctly
   requested `mngr start --restart system-services`.
7. **C3 — unknown preview API routes return the SPA.** `GET /api/nope` returned
   HTTP 200 HTML rather than JSON 404. All ten tested mutation routes returned
   403 as intended. The actual New Tab action tile was inert and preview layout
   changes left the live layout byte-identical.
8. **C5 — re-upping chat leaves the shell preview's copied registry stale.**
   Chat moved from port 39163 to 39073; the shell copy still named 39163. The new
   chat nudger did point at the shell preview. Re-upping the shell repairs the
   registry; merely re-upping chat does not.
9. **C10 — a missing frontend is accepted as a successful sibling boot.**
   Hid the chat worktree's `static/` directory and booted shell with chat. Both
   started and `up` returned 0. HTTP status-only preview health does not check
   `is_frontend_built`. Restored the directory and tore the previews down.
10. **D5 — branch is correct, synchronous completion was not observed.**
    `launch-sync --timeout 120` returned `timed_out: true` and the correct
    `mngr/update-tp1` branch. The worker subsequently delivered its done report.
    This is an observed early-idle/completion-timing limitation, not a verified
    successful synchronous report. Evidence: `d5.json` and the worker report.
11. **E9 / extra no-op check — empty program lists reach supervisor restart.**
    A drop-in-only rollback applied the reread/update (chat pid changed and the
    test environment line disappeared), then called restart without a program
    and logged “restart requires a process name.” It still exited 0. A no-op
    `apply --merge-ref HEAD --keep-rollback-point` also kept an empty-scope point
    naming an existing merge; rolling it back reverted that earlier merge rather
    than doing nothing. Avoid treating no-op apply as a harmless fixture reset.

#### Live scenario ledger

“Partial” means the listed assertions were exercised, but the complete scenario
should not be marked passed. Grouped rows share the stated qualification.

| ID | Status | What was observed |
|---|---|---|
| S0 | Pass with environment notes | Source parity, baseline health, initial clean tree, preview worktree/build, tools and pids recorded. |
| A1 | Pass | Shared healthy observer; chat list and real chat creation work. |
| A2–A3 | Pass | 60-second observer outage retains a 200 list; create during outage appears in chat's own refreshed list; recovery resumes the follower. |
| A4 | Pass | SIGKILL respawns observer; chat pid remains stable; health samples recorded. |
| A5 | Pass | Chat started before observer gives initial-snapshot 503, then 200 after observer starts. |
| A6 | Pass | Missing observer working directory logs fallback and recovers. |
| A7 | Pass with tool note | Vendored all-packages mngr notify uses existing observer; with it stopped, starts a fallback observer; cleaned up afterward. Default root CLI lacks notify plugin. |
| A8–A9 | Pass | Preflight boots without follower; priorities terminal 10, shell 20, observer 24, chat 25. |
| B1 | Fail / partial substitute | Terminal blocked as above; raw HTTP fixture verified multiport/copy/wrapper behavior. |
| B2–B5 | Pass with raw fixture | Missing copy source creates empty directory; invalid bindings/placeholders refuse; wrapper inner path works; refresh retains ports/wrapper and changes inner pid. |
| B6 | Pass on clean retry | Failed refresh exits 1, retains new failure log/pid and same ports/wrapper; subsequent valid refresh recovers. Discarded an earlier harness-interrupted attempt. |
| B7 | Pass | Failed boot preserves logs; bounded timeout reports the failure. |
| B8–B10 | Pass | TERM-resistant teardown escalates in 16.22 s; deregisters; repeated down is safe; refresh without up refuses. |
| C1 | Partial | Real transcript through wrapper; preview send reaches test agent and reply appears live; live chat-data hashes unchanged immediately after preview send; secondary configuration checked. Different-account switching unavailable: only one account exists. |
| C2 | Pass | Missing required instance key refuses. |
| C3 | Fail / partial | API mutations blocked, preview meta/inventory true, actual New Tab tile inert, layout isolation verified; unknown API route fails criterion. Not every menu/picker/stopped-app visual state was individually exercised. |
| C4 | Partial | Sibling boot order/config and rewritten registry checked; nested iframe renders real test chat from preview origin. A separately timed sweep-vs-nudge latency assertion was not collected. |
| C5 | Gap confirmed | Nudger points at shell preview; registry remains on old chat port. |
| C6 | Fail | Terminal sidecar manifest mismatch. |
| C7–C9 | Pass | Unsupported sibling and cross-worktree ownership refuse; re-up preserves sibling association; shell down removes both. |
| C10 | Fail | Missing chat frontend does not fail sibling boot. |
| C11 | Partial | Visible chat composer and shell New Tab changes rebuild/refresh successfully; ports and wrapper retained. Terminal refresh blocked by C6. |
| C12–C13 | Pass | Custom wrapper title checked; nonexistent worktree refuses after removing ownership conflict. |
| D1–D4 | Pass | Held branch refuses without leftover agent; dirty removal refuses; clean handoff uses intended branch/commit; destroy reports it and preserves caller-created branch, including `--delete-branches`. |
| D5 | Partial | Correct branch in JSON; timed-out synchronous result, followed by actual done report. |
| D6 | Pass | Temporarily hid stopped worker's work_dir; start hint names the correct existing branch; restored before destruction. |
| E1 | Pass | Served chat diff names the injected server change and becomes empty after its recovery. |
| E2 | Fail on scope | Successful apply, snapshot paths, notice API and two windows checked; includes shell as well as chat. Snapshot size 1,020 KiB; cached worker-bundle apply 28 s. |
| E3 | Pass | “Everything seems good” returns 204, removes record/copies and buttons in both windows without reload; repeated POST returns 409. |
| E4 | Fail on concurrency / partial recovery pass | Both concurrent requests accepted; initial commit failure; uncontended browser rollback subsequently succeeds with outcome, reconnection and Close in both windows. Scope is both chat and shell. |
| E5 | Partial | Shell banner, real dialog, successful browser rollback and reconnection; chat also restarted because both bundles are in the point. |
| E6 | Pass | Shared UI apply records both apps/programs; rollback restarts both and leaves terminal pid unchanged; outcome and cleanup succeed. |
| E7 | Pass | Terminal-only band and restart; chat/shell pids unchanged on rollback; tmux session listing identical before/after. |
| E8 | Partial / UI gap | Classification and CLI rollback correct, pids unchanged, restart instruction present in outcome; empty apps hides the notice/dialog. |
| E9 | Partial / warning | Drop-in env line reverted and chat reread/update changed its pid; empty restart invocation emits warning. |
| E10 | Pass on records | Kept apply replaces merge SHA; plain final apply removes prior notice and leaves no new one. Snapshot deletion also checked through E3. |
| E11 | Pass, deterministic lock test | With actual rollback lock held, apply exits 1 with expected text; HEAD unchanged and no marker created. |
| E12 | Pass | No-point/already-settled/dirty-tree/lock-held refusals; confirm during rollback refuses; no-point confirm succeeds; rollback while an actual apply pid is alive refuses. |
| E13 | Pass | Synthetic partway record refuses CLI rollback; both UI verbs disappear; CLI confirm closes record. Restored normal record to exercise E3. |
| E14 | Pass | Syntax error returns 2 in 14.18 s with all live pids unchanged. Bootable chat with instances API permanently 503 returns 2 in 290.23 s, names failing API, restores healthy services, leaves no point. |
| E15 | Fail for bundle variant | Empty kept chat bundle yields missing UI but rollback returns 0 and no emergency. Tool-environment sabotage / actual exit-3 recovery was not run: this image's active root-venv launchers are not captured as uv-tool snapshots. |
| E16 | Partial | Plain `--ff-only` apply succeeds with no notice/marker/snapshots. Direct apply does not create `run.json`; the separate update-self run-status orchestration was not exercised. |
| E17 | Pass | Restarted chat after all three services were RUNNING and both HTTP probes answered 200. Restart succeeded; apply waited and exited 0 in 40.46 s. First socket-unavailable attempt did not count. |
| E18 | Pass | A second worktree's preview boots while the previous update notice exists; pending API retains that notice. |
| F1 | Pass | Preview inner and wrapper pids have oom_score_adj 200 (user band). |
| F2 | Pass | No connected clients yields 412; same open succeeds after browser reconnects and establishes its socket. |
| F3 | Recorded | Frontend-only kept snapshots total 1,020 KiB; active uv-tool snapshots were unavailable in this image. |
| F4 | Partial / plan correction | Shell copy contains preview chat URL/label and browser follows it. `layout.py list` does not report URLs; `forward_port.py --list` is not a supported command. |
| F5 | Fail | Generated runner missing import; no fix made. |
| F6 | Pass | All five invalid manifest cases refuse with expected diagnostics. |
| X1 | Partial, blockers recorded | Lease, edit, preview, refresh, commit, teardown, stand-in worker handoff/report, apply and notice actions exercised. Scope/concurrency failures prevent a clean full-loop pass. |
| X2 | Partial | Visible shell preview edit/refresh and separate shell apply/rollback/confirm exercised; no separate full hardening run. |
| X3 | Pass with stand-in qualification | Sibling preview and shared-UI apply/rollback/confirm exercised. |
| X4 | Blocked / live half passed | Preview blocked by C6; terminal live rollback and tmux survival passed. |
| X5 | Pass | Abandon/teardown, existing-branch `-b` refusal, resume with existing branch, clean remove. |
| X6 | Pass | Actual second test chat saw the lease in `tk ready`, inspected/released it, and tore down shell plus chat previews. Orchestrator removed the test worktree. |

#### Harness and plan corrections

- Use `uv run python system/scripts/layout.py`; bare Python in this image lacks
  `tomlkit`. Use the registry file for port listings, not `forward_port.py --list`.
- Current bootstrap code is under `system/libs/bootstrap/`, not the obsolete
  `system/scripts/bootstrap*` example.
- When invoking apply through `docker exec`, copy runtime configuration **without
  `MNGR_AGENT_ID`, `MNGR_AGENT_STATE_DIR`, or `MNGR_AGENT_NAME`**. Inheriting the
  services agent's identity made the first apply kill its own test driver on
  restart. Startup recovery correctly reverted that interrupted attempt. Later
  runs removed those identity variables.
- The snapshot locator warns that the root `.venv` entrypoints are not installed
  uv tools on PATH. Frontend snapshots work, but this run cannot claim automatic
  tool-environment-copy coverage for the staging launcher configuration.
- `c3-ui.json` contains an initial click on the existing Terminal rail row, not
  the New Tab action tile. The corrected assertion is `c3-ui-verified.json`.
- A synthetic marker initially used a plain Python pid, which is correctly
  ignored by the Linux command-line/PID-reuse guard. `E12-actual-live-apply` is
  the valid test, performed against a real running update-self process.


#### Cleanup verification

- Final staging HEAD: `d2f0af3ca4f36c7e28f59a8951cbfe4baf9e547b`.
  `git status --porcelain` and `git diff --name-only f362d9ea8 HEAD` are both
  empty: all tracked source content is back to its starting state. The intentional
  test/recovery history remains for diagnosis; no reset/rewrite was used.
- Chat and shell health are 200 with `is_frontend_built: true`; chat follows the
  healthy observer. The original Welcome transcript renders with its composer.
- Only the main worktree remains. No preview registry rows or isolated-instance
  records, no test workers/chats, no test terminal instance, and no active editing
  lease remain. Original Welcome and system-services agents were preserved.
- No `last-good.json`, `marker.json`, `emergency.json`, or snapshots directory
  remains. The final browser shows no update notice. Private browser and forwarding
  adapter were stopped. Test branches and diagnostic artifacts were retained.
- The corrected E17 injection occurred at `1789760839.200` after RUNNING/200
  observations; `supervisorctl restart chat` completed successfully at
  `1789760844.344`. The final apply completed successfully after 40.46 seconds.
  Timings and pids are in `E17-healthy-before-restart`, `E17-live-chat-restart`, and
  `E10-E16-plain-ff` in `components.jsonl`.
- A portable evidence archive is also saved in the local mngr checkout at
  `.test_output/criticaltest-2026-09-18.tar.gz`. It includes the result document,
  top-level logs/XML/screenshots and test-driver scripts, excluding dependency
  caches and large scratch worktrees.
