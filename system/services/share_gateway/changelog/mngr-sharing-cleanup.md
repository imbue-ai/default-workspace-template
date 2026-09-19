Shared workspaces now hand every backend the requester's whole identity in one header, and grants can name accounts rather than only emails:

- `X-Share-Owner` and `X-Share-Email` are replaced by a single `X-Imbue-Identity` header, a JSON record carrying `owner`, `user_id`, `email`, and (when the account has them) `display_name` and `avatar_url`. The owner's requests carry the owner's own record over a share; the desktop no longer writes `data/.state/share/owner_email`. caddy strips any client-supplied copy before authorization and injects only the value `/_auth/verify` verified, exactly as before.

- The session cookie carries the whole identity record, copied from the broker's handoff token (which gained `display_name` and `avatar_url`). A cookie minted before the record carried a user id opens no session: an HTML navigation is silently bounced through the broker again.

- `share_grants.toml` scopes gain a `users` list matched (by user id) before `emails` and `email_domains`. An `emails` entry is now an invitation: once a visitor with that verified email is admitted, the gateway rewrites the document to hold their user id instead, under an exclusive `flock` on `data/.secrets/share_grants.toml.lock` that every writer of the file holds.

- `/_auth/refresh?next=<url>`, served at every origin of the share, re-runs the broker handoff so a user who changed their name or avatar gets a session carrying the new record.
