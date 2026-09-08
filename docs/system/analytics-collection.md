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

What is collected and how it is redacted is specified in the mngr repo:
`specs/minds-analytics/disclosure.md` (the plain-language description) and
`specs/minds-analytics/redaction-contract.md` (the exact per-field
dispositions).
