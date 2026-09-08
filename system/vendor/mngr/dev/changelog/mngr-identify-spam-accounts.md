Add `scripts/bulk_delete_accounts.py`, an incident-scale operator tool for bulk-revoking sessions and bulk-deleting Imbue Cloud accounts (built for the August 2026 signup-spam cleanup of ~200k accounts).

Unlike `scripts/delete_accounts.py` (one account at a time, serial HTTP), it cleans the connector DB with set-based SQL (temp table + one `DELETE ... USING` per table, plus a bulk pool-lease abort check) and fans the per-user SuperTokens core calls (`/recipe/session/remove`, `/user/remove`) across a worker pool with retry/backoff and a resumable JSONL progress log.

Dry-run by default; `--execute` required to change anything. LiteLLM internal users are deliberately out of scope (verify separately; use `delete_accounts.py` for stragglers).

Also fix the pool-host safety guard in both `scripts/bulk_delete_accounts.py` and `scripts/delete_accounts.py`: instead of enumerating "held" pool-host statuses (an allowlist that silently under-matched newly-added lifecycle states like `stopping`/`starting`/`stopped`/`crashed`), the guard now aborts if any `pool_hosts` row still names the account at all. A full release deletes the row, so any surviving row means an incomplete release in some status -- making the check fail-safe against future status additions.
