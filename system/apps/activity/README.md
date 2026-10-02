# activity

The Activity app ("System Monitor" on the desktop): what is using the workspace's memory, on one page a window of the desktop frames at the
app's own origin. It answers the question a user has ("is my workspace running out of room, and what can I do about
it?") in their terms first, and keeps the raw readings one click away.

- A headline: memory in use against the workspace's limit, with a plain status (room to spare, getting tight, about
  to start closing things) and where the closing starts.
- What is using it, grouped as the user knows it: chats (every harness), the desktop's apps, and background services.
  An idle chat can be stopped from here through the chat app's own stop; it starts again on its next message.
- The process each figure is made of, with its command line and shedding priority, and which process earlyoom would
  most likely shed first.

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
- `POST /api/chats/<chat_id>/stop` and `/start`: forwarded to the chat app's own routes.
- `GET /_static/app_contract.js`: the shell's browser-side contract module, served from this origin as every app
  serves it.

Only the workspace's owner reaches the API (the `X-Imbue-Identity` header's `owner` flag; a request with no header comes
from inside the workspace and counts as the owner), and a write must be JSON from the app's own origin.

The manifest declares `stop_when_no_windows`: the app holds nothing between requests, so the shell stops it a minute
after its last window closes and starts it on the next request. The page polls only while its window is shown
(`shell:shown` / `shell:hidden`).

## Answering a question asked from this page

The memory tab's questions open short answers in place; "Ask in chat" drafts, unsent, a message into the user's chat
that starts with the user's question followed by "I'm looking at System Monitor" and quotes what the page shows. An agent answering it:

- Reads the live figures rather than the quoted ones, which may be minutes old: `curl -s localhost:8040/api/summary`
  (memory and what uses it). A request from inside the workspace
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
