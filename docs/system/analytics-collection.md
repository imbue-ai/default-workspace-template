# data/.imbue/analytics/ -- workspace analytics collection

Workspaces belonging to accounts on the **explorer plan** (and only those)
are periodically visited by Imbue's analytics collection service. This
directory is its footprint inside the workspace, and it exists so the whole
process is auditable from in here:

- **Nothing analytics-related ships in this template.** Roughly once an hour
  while the workspace is online, the collection service connects over SSH
  (with the same pool key that provisioned the workspace), writes the
  then-current collection script into `data/.imbue/analytics/`, and runs it
  there. The script you can read in this directory is exactly the code that
  last ran.
- `collect.py` (plus the `imbue/analytics/injected/` modules beside it): the
  collection script. All transcript redaction -- structural stripping of
  tool inputs/outputs, secret scanning with the workspace's pinned
  betterleaks/kingfisher binaries, and PII scrubbing -- runs here, inside
  the workspace, before anything leaves it.
- `cursors.json`: where each data feed's previous collection stopped
  (byte offsets into event logs, the last collected git commit).
- `collections.jsonl`: one appended record per collection run -- when, which
  sources, how much data, which script version. The service keeps a matching
  server-side audit record for every attempt.

Collection is revocable at any time: remove the pool key's line from BOTH of
the workspace's authorized_keys files -- this workspace's sshd reads
`~/.ssh/authorized_keys` and `/root/.ssh/authorized_keys` (see
`/etc/ssh/sshd_config.d/60-workspace-root-keys.conf`), and the pool key is
listed in each -- and collection simply fails and the workspace is skipped
(this may affect explorer-plan benefits). Leaving the explorer
plan stops collection at the next poll; workspaces on other plans, local
workspaces, and self-hosted workspaces are never touched.

Two of the feeds the script reads are written by this template's own
services, whatever the plan: the share gateway's per-minute request counts
(`events/gateway_requests/events.jsonl` under the services agent: which app,
who asked, read or write, allowed or why not, never a path or an address;
`system/services/share_gateway/README.md`) and the shell's `window_opened` and
`window_input` client-activity events (which app a window was opened on,
and which app you or a visitor interacted with in a given minute: the app and
the window, never the path, the title, or what was typed or clicked). The
gateway's per-request access log in `data/.state/share_gateway/access.log` is
yours alone and is never collected.

The script also reads how hard the machine is working, from what this
template's System Monitor sampler and memory guard already record
(`system/services/oom_priority/README.md`), plus a reading it takes itself:

- the per-minute CPU and memory averages in
  `data/.state/system-monitor/history.jsonl`;
- the busy-CPU and high-memory stretches and the ten-minute snapshots of
  which chats and programs hold the most memory
  (`data/.state/system-monitor/events/`). A chat is named by its agent id. A
  program is named only when this template provides it (its supervisord
  program exists in the template commit the workspace was created from or
  updated to) or when it is a registered app, whose name the workspace-state
  snapshot already carries; any other program is reported only as "a user
  program";
- the memory guard's record of each process it stopped
  (`data/.state/oom_priority/events/shed.jsonl`): when, how much memory it
  held, its shedding priority, and whether it was an agent. The process name
  is reported only when it is a common runtime (`claude`, `node`, `python3`,
  `chromium`, and the like), otherwise as `other`; the process id and the
  agent's name are never collected;
- one reading per collection run: CPU cores, memory total and available (as
  the machine and as the container see them), disk quota and free space,
  `/tmp` usage, and uptime.

What is collected and how it is redacted is specified in the mngr repo:
`specs/minds-analytics/disclosure.md` (the plain-language description) and
`specs/minds-analytics/redaction-contract.md` (the exact per-field
dispositions).
