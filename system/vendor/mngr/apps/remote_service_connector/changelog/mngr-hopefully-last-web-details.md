Re-enabling sharing on a leased host no longer overwrites the workspace's sharing-grants document: the connector's enable-sharing (and the claim path that composes it) now seeds `share_grants.toml` only when absent, while still replacing `share.env` every time (each enable rotates the relay token). Previously a re-enable reset the grants to the owner-only seed, silently revoking every grant the user had added since.

The web chrome's owner-exec client (`ExecClient.getGrants`/`putGrants`) now speaks the grants compare-and-swap contract: reads return the document's revision, conditional writes send it as `base_revision`, and a lost race surfaces as `GrantsConflictError` carrying the current document for merge-and-retry.

`PUT /sync/bundle` gained a create-only mode (`?if_absent=true`, 409 `bundle_exists` when a bundle is already stored), and the chrome's first-time master-password setup uses it: two tabs (or devices) racing to mint the account's first DEK can no longer silently clobber each other -- exactly one wins, and the losing tab is told to unlock with the winning password instead of holding a key no stored bundle could ever recover. Plain (unconditional) puts are unchanged, so the change-password flow still replaces in place.

The chrome's unlock flow now distinguishes a structurally damaged key bundle (undecodable/truncated `wrapped_dek`) from a wrong master password, with a distinct message for each -- re-typing the password can never fix a damaged bundle.

New edge-case tests: web-create claim retry after a partial adopt (including a stuck teardown), crossed desktop+web record edits inside one CAS window, concurrent-unlock DEK-store races, and the overview tile state for a shared workspace whose tunnel is dead (renders "unreachable", never "desktop-only").
