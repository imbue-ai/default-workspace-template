# activity

The Activity app ("System Monitor" on the desktop): what is using the workspace's memory and disk, on one page a window of the desktop frames at the
app's own origin. It answers the question a user has ("is my workspace running out of room, and what can I do about
it?") in their terms first, and keeps the raw readings one click away.

- A headline: memory in use against the workspace's limit, with a plain status (room to spare, getting tight, about
  to start closing things) and where the closing starts.
- What is using it, grouped as the user knows it: chats (every harness), the desktop's apps, and background services.
  An idle chat can be stopped from here through the chat app's own stop; it starts again on its next message. An app
  can be stopped through the desktop's own Quit (its windows close, and it starts again when next opened), except a
  critical one, the browser (agents drive it) and System Monitor itself.
- The process each figure is made of, with its command line and shedding priority, and which process earlyoom would
  most likely shed first.
- Memory over time (the last hour, day or week), with the point where closing starts, a mark wherever the memory
  guard closed something, and a plain list of what it closed in the last week and what to do about each.
- A warning while memory has stayed tight for five minutes or more (with since when), and a quieter note for an hour
  after it eases. It is worked out from the recorder's history plus the live reading whenever the page reads the
  summary (`activity.pressure`), so it is seen when System Monitor is open; nothing notifies the user while it is
  closed.

It runs as the `activity` supervisord program (`system/supervisord.conf.d/activity.conf`) from its own uv tool
environment (`system/scripts/build_workspace.sh`), serving on `http://127.0.0.1:8040`:

- `GET /`: the page (the frontend's build, `src/activity/static/index.html`; a placeholder until it is built), with its
  bundle under `/assets/`.
- `GET /api/health`: `{"status", "is_frontend_built"}`.
- `GET /api/summary`: the memory tab, read fresh on every request. Memory comes from `/mngr-vol/.host-meminfo` on a
  cloud workspace, the cgroup v2 files otherwise, and `/proc/meminfo` last; the reading names its source. Processes
  come from `/proc`, credited to chats through the agent-pid registry (`system/services/oom_priority`), to apps and
  services through supervisord's XML-RPC socket (`getAllProcessInfo`), and to the chat titles and harnesses the chat app's `GET /api/chats`
  reports. A source that cannot be read becomes a note, never an empty list.
- `GET /api/storage`: the storage tab: one `du -sk` over disjoint folders grouped into categories (the user's files
  under `data/`, chats and agents, app data, installed tools, download caches, logs), measured when asked, never on a
  timer. No limit is reported: `df` inside the container sees the host's disk, and a cloud quota is not published in.
- `GET /api/history?range=hour|day|week` (default `day`): the memory-over-time chart. Readings grouped into periods
  (a minute, five minutes, half an hour), each with its average, lowest and highest, so a spike survives grouping; a
  period with no reading is left out, so a gap in recording stays a gap. Also the closures in range and the last
  week's, read from earlyoom's shed ledger (`oom_priority.paths.shed_ledger_path()`) and described in plain words.
  Closures the kernel makes at the container's own limit are not recorded anywhere and so are not shown.
- `POST /api/chats/<chat_id>/stop` and `/start`: forwarded to the chat app's own routes.
- `POST /api/apps/<name>/stop`: forwarded to the shell's `POST /api/apps/<name>/quit` (at `shell_base_url()`, as
  every app reaches the shell) once the app is checked against the same rule the page offers Stop by
  (`activity.apps.is_app_stoppable`: the shell's own refusals, plus the browser and this app), since the page's view
  may be stale and a request need not come from it.

A preview (`activity-app --no-register`) reads the live workspace, so both stops answer it `403` in the shell
preview's words and the page offers neither.
- `GET /_static/app_contract.js`: the shell's browser-side contract module, served from this origin as every app
  serves it.

Only the workspace's owner reaches the API (the `X-Imbue-Identity` header's `owner` flag; a request with no header comes
from inside the workspace and counts as the owner), and a write must be JSON from the app's own origin. A preview
(`activity-app --no-register`, reading the registry copy `MINDS_APPS_FILE` names) shows the live workspace, so every
write answers it `403` in the shell preview's words and the page offers no actions.

The manifest declares `stop_when_no_windows`: the app holds nothing between requests, so the shell stops it a minute
after its last window closes and starts it on the next request. The page polls only while its window is shown
(`shell:shown` / `shell:hidden`).

The chart's readings come from `activity-record-memory`, a cron job rather than a resident program, so recording costs
no memory between readings: once a minute it appends one line (`<epoch seconds>\t<used KiB>\t<limit KiB>\t<source>`;
readers ignore columns they do not know, and read a line without a source as one from an unknown source) to
`data/.state/activity/memory-history.tsv` and exits, dropping readings older than a week once the file passes 512 KiB.
The app installs its entry, `/etc/cron.d/activity-memory-history`, each time it starts (at every boot, since its program
autostarts), under `flock -n` and `timeout 50`, so a stalled read never stacks up a recorder a minute. It is code-owned, so it has no copy in the user-editable `data/.state/cron.d/`, and it skips
`with_agent_env.sh` because the recorder needs no agent credentials. The recorder logs to
`/var/log/supervisor/activity-record-memory.log`. A reading is the same one the headline uses, so the chart and the
headline agree.

## Answering a question asked from this page

The memory tab's questions open short answers in place; "Ask in chat" drafts, unsent, a message into the user's chat
that starts with the user's question followed by "I'm looking at System Monitor" and quotes what the page shows. An agent answering it:

- Reads the live figures rather than the quoted ones, which may be minutes old: `curl -s localhost:8040/api/summary`
  (memory and what uses it), `curl -s 'localhost:8040/api/history?range=day'` (how it has gone, and what was closed)
  and `curl -s localhost:8040/api/storage` (disk). A request from inside the workspace
  carries no identity header and counts as the owner. If the app was stopped, the shell answers the first request
  with a 503 "starting" page while it wakes the app; retry after a second or two.
- Explains in the page's terms: chats, apps, background services. "Likely closed first" is the predicted next pick of
  whichever closes things first (earlyoom at its `-m` threshold, or the kernel at the container's cgroup limit), by
  the fork's badness (`oom_priority.badness`), not a certainty.
- Answers "how do I free memory" by `.agents/shared/references/freeing-memory.md`: list the candidates, let the user
  choose, and stop nothing on their behalf. A stopped chat keeps its conversation and picks up when messaged;
  helper agents it started keep running.
- Answers "can I get more memory" plainly: not from inside the workspace. A cloud workspace's machine size is shown,
  read-only, in Imbue Studio's workspace settings ("Machine size"), and a pending resize applies on restart; a local
  workspace's size is set when it is created.
