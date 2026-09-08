`scripts/delete_accounts.py` now deletes the account's `account_attribution`
row along with the rest of its connector-DB rows.

The table was added to `scripts/bulk_delete_accounts.py` but never to the
single-account tool, so a "full" single-account deletion silently left behind a
row carrying the deleted account's email and its marketing first/last touches.
Which of the two operator tools you happened to reach for decided what
survived.

A new test pins the two tools' target-table tuples to each other, so the pair
cannot drift apart again.

Deleting the row has a cost the tool's docstring now records: for an account
created after the static SuperTokens backfill, `account_attribution` is
analytics' only record of the signup, and `gold.accounts` / `gold.funnel_daily`
are rewritten in full on every aggregation run -- so the next run drops the
account from the accounts dimension and its signup from the funnel counts for
every day of history, not just recent ones. The bulk tool has always behaved
this way; the single-account tool now matches it.
