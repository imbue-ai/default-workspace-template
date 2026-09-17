---
name: access-openobserve
description: Reach and query the self-hosted per-tier OpenObserve holding server-side minds logs and metrics -- per-request access logs from the Modal apps plus box/relay/instance logs -- over an SSH tunnel with latchkey-injected credentials, using SQL `_search`. Use to check whether the server was serving at a given time, reconstruct a user's server-side request timeline, or read service logs and fleet metrics.
---

# Accessing OpenObserve (server-side logs and metrics)

OpenObserve is a self-hosted log/metric store with one instance per tier: `production`, `staging`, and a `dev` instance shared with CI.
Everything lives in the single `default` organization.
Log streams split by source: `modal_logs` (connector, LiteLLM proxy -- one JSON object per line), `box_logs` (bare-metal boxes), `relay_logs` (share relays), and `instance_logs` (the instance itself).
Each OTLP metric becomes its own stream; stream-level endpoints for metrics need `?type=metrics`.
Logs are retained for a much shorter window than metrics.

The workhorse record is the `http_request` access-log line in `modal_logs`; its `user` field is the SuperTokens user id (present only when the route stashed it), `path` has query strings deliberately stripped, and `minds_env` says which env emitted the line on shared instances.
Error tracebacks appear as `level: ERROR` lines with an `exception` field -- the raw-log complement of the grouped Bugsink event.

Absent by construction: emails (search by user id), anything client-side, anything from inside workspace VMs, and requests that never reached the server.
A Modal outage can leave gaps in `modal_logs`; box/relay collectors buffer through outages.

## Transport: SSH tunnel

The API and UI listen only on the instance's loopback; the public hostname accepts only OTLP ingest, and it resolves to Cloudflare's proxy rather than the instance, so DNS cannot substitute for the instance IP.
Find the IP with `just list-observability-instances` (tier follows the activated minds env; OVH credentials resolve from Vault), which prints one row per instance including `ip`.
The SSH user is `debian`; the key is the `OBSERVABILITY_SSH_PRIVATE_KEY` leaf in the tier's `observability` Vault entry (Vault mechanics: `apps/minds/docs/deploy/vault-setup.md`).
Vault grants are per-tier, and the vault CLI holds one login's token at a time -- confirm the active token matches the tier before fetching.
Fetch the key per session rather than installing it (same pattern as `access-bugsink`, including the trailing-newline guard):

```bash
TMPD=$(mktemp -d); trap 'rm -rf "$TMPD"' EXIT
vault kv get -mount=secrets -field=value minds/<tier>/observability/OBSERVABILITY_SSH_PRIVATE_KEY > "$TMPD/key"
chmod 600 "$TMPD/key"
tail -c 1 "$TMPD/key" | od -An -c | grep -q '\\n' || printf '\n' >> "$TMPD/key"
ssh -i "$TMPD/key" -o IdentitiesOnly=yes -N -L <local-port>:127.0.0.1:5080 debian@<instance-ip>
```

Local port convention, so per-tier latchkey credentials can never cross tiers: production 5080, staging 5081, dev 5082.

## Query API through latchkey

The latchkey services `openobserve-production`, `openobserve-staging`, and `openobserve-dev` are registered against those localhost ports and hold personal credentials.
Search is `POST /api/default/_search` with a SQL body; **times are epoch microseconds** and both bounds are required:

```bash
latchkey curl -s -X POST 'http://localhost:<local-port>/api/default/_search' \
  -H 'Content-Type: application/json' -d '{
  "query": {
    "sql": "SELECT _timestamp, spath(body, '\''path'\'') AS path, spath(body, '\''status'\'') AS status FROM modal_logs WHERE spath(body, '\''type'\'') = '\''http_request'\'' ORDER BY _timestamp DESC",
    "start_time": <epoch-micros>, "end_time": <epoch-micros>, "from": 0, "size": 100
  }
}'
```

`spath(body, 'field')` extracts fields from the JSON log body; journald-derived lines (box/relay/instance streams) put their text in `body_message` instead.
Aggregates work:

```sql
-- Was the server serving in a window?
SELECT spath(body, 'status') AS status, count(*) AS n FROM modal_logs
  WHERE spath(body, 'type') = 'http_request' GROUP BY status ORDER BY n DESC
-- How busy, and with whom?
SELECT count(DISTINCT spath(body, 'user')) AS active_users, count(*) AS requests FROM modal_logs
  WHERE spath(body, 'type') = 'http_request'
-- One user's server-side timeline:
SELECT _timestamp, spath(body, 'path') AS path, spath(body, 'status') AS status FROM modal_logs
  WHERE spath(body, 'type') = 'http_request' AND spath(body, 'user') = '<supertokens-user-id>'
```

Getting the user id from an email is an operator-gated lookup; see the correlation-keys table in `investigate-bug`.

## UI

Open `http://localhost:<local-port>` over the same tunnel and sign in with your personal credentials.

## Minting or replacing personal credentials

Use the `service_account` role: it is the least-privileged role that can run `_search`, but it is not read-only (it can also ingest), so treat the credential as sensitive.
Mint with the root account from the tier's `observability` Vault entry (`OPENOBSERVE_ROOT_EMAIL` / `OPENOBSERVE_ROOT_PASSWORD`), without printing any secret:

```bash
curl -s -u "$ROOT_EMAIL:$ROOT_PASS" -X POST http://localhost:<local-port>/api/default/users \
  -H 'Content-Type: application/json' \
  -d '{"email":"<you>@imbue.com","password":"<generated>","role":"service_account","first_name":"<You>","last_name":"latchkey"}'
latchkey auth set openobserve-<tier> -u "<you>@imbue.com:<generated>"
```

Revoke by deleting the user with the root account.

This skill is a living document; when information here is stale, follow the update protocol in `investigate-bug`.

## Related skills

- `investigate-bug` -- routing, correlation keys, and interpreting zeros.
- `access-bugsink` -- the grouped-exception complement of the ERROR log lines here.
- `access-sentry` -- the client-side error store.
