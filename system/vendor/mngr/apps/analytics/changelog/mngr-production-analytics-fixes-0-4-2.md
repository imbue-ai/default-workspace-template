# Analytics data-quality fixes from the production data review

- The `workspace_git_commit` activity signal now counts only commits unique to one workspace: commit shas collected from multiple workspaces are shared template/upstream history (every workspace clone carries the template repo's commits), not code the user produced, and previously dominated the signal.

- The `signup` activity signal and `funnel_daily.signups` now apply the signup-timestamp rule from the bringup runbook: `gold.accounts_signup` (the static SuperTokens backfill) coalesced with `account_attribution` for accounts created after the backfill. Previously only attribution was read, so all signups before 2026-08-17 were invisible. The aggregation now ensures the `accounts_signup` table exists (empty where no backfill ran).

- `gold.accounts` is rebuilt as a real dimension: its spine is every account id any signup source knows (backfill, entitlements, attribution) instead of only the lazily-created entitlements rows; it gains `signup_at` (the coalesced real signup moment) and `is_suspended` (from `account_entitlements.suspended_at`), and the entitlements row timestamps are renamed to `entitlements_created_at` / `entitlements_updated_at` to stop them reading as account-creation dates.

- New `share_enabled` activity signal: enabling sharing for a workspace, mapped from the share record's 32-hex owner label back to the full SuperTokens account id.

- `funnel_daily` is now written over a full day spine (first source day through last), so days where nothing happened appear as zeros instead of being silently absent.

- The aggregation cron retries once (via tenacity, newly added to the image) when a read races OpenObserve's parquet compaction (an object 404s between glob listing and read), logging a warning before the retry so retried failures stay visible.

- The worked-example reports exclude operator-suspended accounts via the new `is_suspended` flag, and `reports/README.md` documents each source's data start date (activity table 2026-08-19, structured log lines 2026-08-25, download events 2026-08-21, collection 2026-08-26).

- `logs.http_requests` gains an `imbue_client` column (the X-Imbue-Client self-identification header, empty for clients older than 0.4.1), so client-version questions are answerable from the log views without raw parquet queries. `reports/README.md` also documents that pre-fix `servers`-feed raw rows share one event id fleet-wide and must be deduped by host.

- The in-workspace redaction pipeline gains a third scrub step: random-looking identifier tokens (UUIDs, 16+ hex runs, 7+ digit runs, high-entropy token shapes) in message text are replaced with `[REDACTED_TOKEN]`, catching the identifier-shaped residue (meeting ids, receipt numbers, tracking blobs) that secret scanners and Presidio do not classify. Workspace-local paths (`/home/user...`, `~/...`) are kept whole; other path-like strings are scrubbed per segment. Measured on all collected production messages: only 1.6% contain any such token, so reading signal is essentially untouched. The contract change is documented in `specs/minds-analytics/redaction-contract.md`.

- The injected collection script now reports a wholesale-missing workspace layout (old workspace generations keep their data at other paths): the run summary carries a `workspace_layout` error entry that lands in the server-side `collection_runs` audit detail, and the workspace_state snapshot gains `is_workspace_repo_present` / `is_host_agents_dir_present` markers, so a collection that connects but reads an empty world is visible instead of indistinguishable from a healthy run.
