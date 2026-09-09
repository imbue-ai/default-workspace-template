---
name: access-bugsink
description: Reach and query the self-hosted per-tier Bugsink error tracker holding server-side minds exceptions -- connector project `rsc`, LiteLLM proxy project `llm` -- over an SSH tunnel with latchkey-injected credentials. Use when a minds failure reached the server: an `internal_error` response carrying an event_id, a suspected connector or LLM-proxy exception, or an infrastructure alert.
---

# Accessing Bugsink (server-side error tracking)

Bugsink is a self-hosted, Sentry-compatible error tracker with one instance per tier: `production`, `staging`, and a `dev` instance shared with CI.
It receives unhandled exceptions and WARNING-and-above logs from the Modal services: the remote-service connector (project `rsc`) and the LiteLLM proxy (project `llm`).
The connector's 500 handler embeds the Bugsink `event_id` in `internal_error` response bodies, so a client-visible 500 jumps straight to its server-side event.
The connector's box-health check also raises infrastructure issues here.
Events are retained for a bounded window and capped per project; issue rows (title, counts, first/last seen) persist longer.
Events carry `environment` (the concrete env name, else the tier), `release` (the deploy id), and `service` -- and no PII, so there are no emails or user ids to search by.
Client-side failures and requests that never reached the server are absent by construction; see `investigate-bug`.

## Transport: SSH tunnel

The API and UI listen only on the instance's loopback; the public hostname accepts only Sentry-protocol ingest, and it resolves to Cloudflare's proxy rather than the instance, so DNS cannot substitute for the instance IP.
Find the IP with `just list-bugsink-instances` (tier follows the activated minds env; OVH credentials resolve from Vault), which prints one row per instance including `ip`.
The SSH user is `debian`; the key is the `BUGSINK_SSH_PRIVATE_KEY` leaf in the tier's `bugsink` Vault entry (Vault mechanics: `apps/minds/docs/deploy/vault-setup.md`).
Vault grants are per-tier, and the vault CLI holds one login's token at a time -- confirm the active token matches the tier before fetching.
Fetch the key per session rather than installing it:

```bash
TMPD=$(mktemp -d); trap 'rm -rf "$TMPD"' EXIT
vault kv get -mount=secrets -field=value minds/<tier>/bugsink/BUGSINK_SSH_PRIVATE_KEY > "$TMPD/key"
chmod 600 "$TMPD/key"
# ssh rejects a key file without a trailing newline:
tail -c 1 "$TMPD/key" | od -An -c | grep -q '\\n' || printf '\n' >> "$TMPD/key"
ssh -i "$TMPD/key" -o IdentitiesOnly=yes -N -L <local-port>:127.0.0.1:8300 debian@<instance-ip>
```

Local port convention, so per-tier latchkey credentials can never cross tiers: production 8300, staging 8301, dev 8302.

## API access through latchkey

The latchkey services `bugsink-production`, `bugsink-staging`, and `bugsink-dev` are registered against those localhost ports and hold personal API tokens.
With the tunnel up, omit the Authorization header and let latchkey inject it:

```bash
latchkey curl -s 'http://localhost:<local-port>/api/canonical/0/projects/'
latchkey curl -s 'http://localhost:<local-port>/api/canonical/0/issues/?project=<id>'
```

`/projects/` lists each project's slug, DSN, and event counts.
`/issues/?project=<id>` returns paginated issue rows; the human-readable title is `calculated_type` plus `calculated_value`.

## Minting or replacing a token

Tokens are minted on the instance; they are unscoped and unlabeled, so keep one per person and revoke via the admin UI when needed.

```bash
ssh -i "$TMPD/key" debian@<instance-ip> \
  "sudo bash -c 'set -a; . /etc/bugsink/bugsink.env; set +a; cd /opt/bugsink && venv/bin/bugsink-manage create_auth_token -v 0'"
latchkey auth set bugsink-<tier> -H "Authorization: Bearer <token>"
```

Capture the token into a shell variable and pipe it straight into `latchkey auth set` without printing it.

## UI

Open `http://localhost:<local-port>` over the same tunnel.
Sign in with your own account, or break-glass with the `CREATE_SUPERUSER` credential in the tier's `bugsink` Vault entry.

This skill is a living document; when information here is stale, follow the update protocol in `investigate-bug`.

## Related skills

- `investigate-bug` -- routing and correlation keys.
- `access-openobserve` -- the same failure's traceback also appears there as an ERROR log line, with the surrounding request context.
- `access-sentry` -- the client-side error store.
