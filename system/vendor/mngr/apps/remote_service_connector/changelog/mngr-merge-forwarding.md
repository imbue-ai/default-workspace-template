Merge main into the self-hosted sharing branch (the shares/certs/broker endpoints land as described in the sibling entries in this PR), plus review fixes on top:

The per-user share quota is now enforced inside one advisory-locked transaction, so concurrent share creates cannot overshoot it.

Migration `019_sharing_certs.sql` is re-runnable as documented (the dependent relay-token FK is dropped before the shares primary key it references).

The Modal image declares `cryptography` explicitly instead of relying on it transitively.

Further review fixes: a missing Cloudflare sharing secret now surfaces as the 503 sharing-not-configured diagnostic on `/shares/cert` instead of an opaque 500; migration 021 indexes `shares.workspace_domain` for the broker's per-visit lookup; share-row projection fails loudly on column-count drift; and stale tunnel wording (including the frps-auth docstring's allow-unchanged claim) was corrected.

Second review pass: share activation and relay-token rotation now run in one advisory-locked transaction (concurrent creates can no longer leave two valid tokens, and a crash can no longer leave an active share with no token); frps `NewProxy` operations claiming a `subdomain` are rejected outright; and the README's authentication section covers the cookie-authenticated accounts-broker routes.

Third review pass: certificate issuance is rate-limited to 5 per share per rolling day (a relay-token holder could loop ACME issuance and burn CA quota); the accounts-broker login form rejects cross-site posts (login CSRF needs no cookie, so SameSite offered no protection); and the tunnel-era `ForwardingCtx` holder is renamed `CloudflareCtx` to match what it still does (R2 bucket ops).
