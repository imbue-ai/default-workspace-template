Documentation: the README's account-deletion section now lists
`account_attribution` among the connector-DB tables `scripts/delete_accounts.py`
removes. That table (one row per account, carrying the account's email and its
marketing first/last touches) was missing from the single-account tool, so the
documented list understated what a "full" deletion leaves behind.
