Implement phases 3-5 of the minds analytics spec: the in-workspace collection loop for explorer-plan workspaces.

- New `collection_poll` cron (every 15 minutes, deploy-time overridable): syncs explorer-plan membership into the new ops `consent_ledger`, enumerates online explorer workspaces from the connector DB, and collects from each at most hourly (per-tier tunable via the analytics secret) under a bounded thread pool with a per-workspace timeout.

- The collection script is injected on every run into `data/.imbue/analytics/` (source of truth: `imbue/analytics/injected/`), executed via `uv run --script` with its own pinned PEP 723 environment, and left in place for audit; every run also appends to the in-workspace `collections.jsonl` and to the server-side `collection_runs` audit (refused hops included).

- ALL transcript redaction runs inside the workspace per specs/minds-analytics/redaction-contract.md: structural strip (tool inputs and outputs dropped entirely), betterleaks + kingfisher secret-line scanning (fail-closed), then Presidio PII scrubbing -- the runner validates the multiplexed JSONL output as untrusted input (size caps, envelope shape) and never remediates content.

- v1 feeds: redacted common transcripts, `client_activity` (chat text dropped at the source), service/server registration events, git `--numstat` counts (no paths, messages, or authors), a workspace-state snapshot (presence booleans and names only), and a probe-based VM latchkey-state signal where a VM gateway exists.

- Raw rows land as typed envelope columns plus a JSON payload column in `metrics.raw.workspace_events` / `transcripts.raw.transcript_events`; cursors are runner-owned and advance only after the matching lake batch commits, with downstream dedupe by event id.

- Aggregation now derives `transcript_daily` / `transcript_tools_daily` and `collection_health` gold tables and adds the explorer activity signals (`workspace_chat_message`, `workspace_git_commit`, `workspace_user_message`); lake maintenance covers the transcripts lake.

- New `deletion.py`: account deletion removes transcript-lake content and writes a `deletion_events` fact row; metrics rows survive keyed by the orphaned opaque id. Physical removal rides the 30-day snapshot expiry.

- Workspace host keys are recorded trust-on-first-use with change detection (adoption rotates them to user-generated keys the server never learns); a changed key flags the audit row instead of blocking collection.

- The revocation instructions (the in-workspace README and the disclosure/spec docs) now name both authorized_keys files the workspace's sshd reads (`~/.ssh/authorized_keys` and `/root/.ssh/authorized_keys`) -- the pool key is listed in each, so removing it from only one does not revoke access (found during the dev bringup validation).
