# What we collect, and from whom

This is the plain-language description of the data the minds analytics
system handles. It is the source of truth for any user-facing copy about
analytics (the accounts surface will link to a published version of this).

## If you are NOT on the explorer plan

We only see what any server operator inevitably sees: the requests your
minds app makes to our services, and the records our product database needs
to run those services (your account, your workspace registrations, your
shares, your downloads). Concretely:

- One log line per request to our services: which endpoint, when, status,
  duration, your account id, client IP, and user agent. Never request
  bodies, never query strings.
- Product records: workspaces created/stopped/started, shares enabled,
  share visits you make to other people's shared workspaces, downloads and
  signups.

We aggregate this into metrics like "how many accounts were active this
week". We do not add client-side tracking of any kind: no telemetry in the
desktop app, no scripts on your pages, no reading inside your workspaces.

Workspaces on your own machine or your own cloud accounts are never
touched: we have no access to them at all.

## If you ARE on the explorer plan

The explorer plan trades free hosted resources for visibility into your
**imbue-hosted** workspaces. Consent is per-account: every imbue-hosted
workspace of an explorer account is observable. If you want a private
workspace, run it locally or under an account on a different plan.

What that visibility means, mechanically:

- Roughly once an hour while a workspace is online, we connect to it over
  SSH and run a collection script. The script is written to
  `data/.imbue/analytics/` inside your workspace before it runs, and the
  latest version stays there afterwards, so you can always read exactly
  what ran. Every run also appends a record to
  `data/.imbue/analytics/collections.jsonl` in your workspace: when, which
  sources, how much data, which script version.
- The script collects: your chat transcripts (redacted -- see below), UI
  activity events (chat sends and view switches), which apps/services are
  registered, high-level git history statistics (per-commit file/line
  counts, not code), and a workspace-state snapshot (sharing on/off,
  installed apps, agent count, template version).
- Transcripts are redacted **inside your workspace before anything
  leaves**: everything your tools read or produced (file contents, command
  output, tool arguments) is removed entirely; message text is scanned for
  secrets and personal information and scrubbed. The exact rules are pinned
  in [redaction-contract.md](./redaction-contract.md).
- You can revoke our access at any time by removing our key from the
  workspace's authorized_keys files. The workspace's sshd reads two of them
  (`~/.ssh/authorized_keys` and `/root/.ssh/authorized_keys`) and our key is
  listed in both, so remove its line from each. Collection then simply fails
  and the workspace is skipped. (This may affect explorer-plan billing
  benefits in the future.)
- Access to collected transcripts is restricted to a small named set of
  product owners; aggregate metrics derived from them are more broadly
  visible inside Imbue.

## Retention and deletion

- Leaving the explorer plan stops collection immediately. Already-collected
  data is retained under the rules below.
- Deleting your account deletes all collected transcript content. Aggregate
  metrics and event metadata survive, keyed by an opaque identifier that no
  longer maps to any person once the account is gone.
- Deleted data is unqueryable immediately and physically removed from
  storage within 30 days.
