# Observability bring-up (OpenObserve): dev, staging, production

Operator runbook for standing up the per-tier log and telemetry aggregation
instances and rolling the fleet collectors out. Design and rationale live in
`specs/minds-openobserve-telemetry.md`; the CLI surface is documented in
`apps/observability/README.md`. Track the per-tier status in
[next_deploy.md](../next_deploy.md).

Order matters: bring up **dev first** and run the validation gates below --
the OpenObserve API shapes and the Modal OpenTelemetry integration are pinned
best-effort in code and the dev pass is what confirms (or corrects) them.
Then staging, let it soak, then production.

## Prerequisites

- PR mngr-internal#465 merged (or run from its branch).
- `vault login` (see [vault-setup.md](vault.md)) and an activated
  env for the target tier (`eval "$(uv run minds-admin env activate <env>)"`).
  Any `dev-*`/`ci-*` env maps to the single shared **dev** instance.
- Access to the tier's Cloudflare account (R2 + origin certs), Neon org, and
  Modal workspace settings (the OpenTelemetry integration is configured in
  the dashboard by hand; there is no API for it).

## Per-tier bring-up procedure

Run this once per tier (dev, then staging, then production).

### 1. Create the backing resources

- **Neon**: create an `openobserve` database. For staging/production, inside
  the tier's existing Neon project; for dev, inside a small dedicated project
  in the dev org (mirroring the shared dev Bugsink instance). Note the DIRECT (non `-pooler`)
  DSN.
- **R2**: in the tier's Cloudflare account, create the
  `minds-observability-<tier>` bucket and an account-owned R2 token scoped to
  that one bucket (R2 -> Manage API tokens). Note the S3 access key id +
  secret.
- **Origin certificate**: Cloudflare dashboard -> SSL/TLS -> Origin Server ->
  create a certificate covering `telemetry.<tier domain>` (the domain is the
  tier's `cloudflare_domain` from its `deploy.toml`, e.g. `minds-dev.com`).
- **SSH keypair**: `ssh-keygen -t ed25519 -f observability_key -N ""`.
- **Root account**: pick an email (e.g. `josh_<tier>@imbue.com`) and a strong
  password satisfying OpenObserve's complexity policy -- at least one
  lowercase letter, uppercase letter, digit, and special character (first
  boot crash-loops otherwise; `openssl rand -base64 24` alone can miss a
  class): `echo "$(openssl rand -base64 24 | tr -d '=+/')aA7!"`.

### 2. Populate Vault

```bash
cp .minds/template/observability.sh /tmp/observability-<tier>.sh
$EDITOR /tmp/observability-<tier>.sh   # fill everything EXCEPT INGEST_CREDENTIAL_* (leave empty)
uv run scripts/push_vault_from_file.py <tier> observability /tmp/observability-<tier>.sh
shred -u /tmp/observability-<tier>.sh
```

The template's comments explain each key. The three `INGEST_CREDENTIAL_*`
leaves stay empty: provisioning mints them and writes them back.

### 3. Provision the instance

```bash
eval "$(uv run minds-admin env activate <env>)"   # tier is derived from this
just provision-observability                # OVH region defaults to US-EAST-VA-1
```

One shot: orders the OVH VPS, installs the pinned OpenObserve + caddy ingest
gate + nftables firewall + self-monitoring collector, mints the three ingest
credentials (written back to Vault), applies log-stream retention, and points
the Cloudflare-proxied `telemetry.<domain>` record at the instance. The
recipe prints the manual follow-ups when it finishes.

**Note:** re-running provisions a NEW VPS. To re-run only the credential
minting / retention pass against the existing instance, use
`just provision-observability-accounts <instance-ip>`.

### 4. Configure the Modal workspace integration (manual)

First create the integration's Modal Secret from the CLI (Modal Secret keys
must be valid environment variable names, which is also why the secret does
NOT carry the hyphenated `stream-name` header -- the caddy gate stamps
`stream-name: modal_logs` on the bare `/v1/*` ingest paths, which only
Modal uses):

```bash
umask 077 && tmp=$(mktemp)
vault kv get -mount=secrets -field=value minds/<tier>/observability/INGEST_CREDENTIAL_MODAL \
    | jq -R '{"OTEL_HEADER_Authorization": .}' > "$tmp"
MODAL_PROFILE=<tier workspace> uv run modal secret create -e main \
    --from-json "$tmp" observability-otel-ingest
rm -f "$tmp"
```

Then in the tier's Modal workspace settings (dev/CI envs share `minds-dev`),
configure the OpenTelemetry integration by hand:

- Endpoint: `https://telemetry.<domain>` -- the BARE base URL, not the
  `/api/default` variant: the bare `/v1/*` paths are what the gate stamps
  the `modal_logs` stream routing onto (an `/api/default`-suffixed endpoint
  still ingests, but its logs land in OpenObserve's `default` stream).
- Secret: select `observability-otel-ingest`.

### 5. Roll the fleet collectors

- Boxes: one `just server-prep <server-id>` pass per box (`minds-admin server
  prep` resolves the tier's boxes credential from Vault in-process, installs
  the pinned otelcol-contrib, and verifies the unit is active -- fail-closed;
  it logs a clean skip while the credential is absent). New boxes get it at
  `just server-setup` / prep automatically.
- Relays (existing ones; new dev relays get this during
  `just provision-dev-relay`):

  ```bash
  tmp=$(mktemp -d)
  uv run python scripts/provision_observability_config.py collector-env <tier> relays "$tmp"
  set -a; . "$tmp/collector.env"; set +a
  uv run observability install-collector --host <relay-ip> --role relay --tier <tier> \
      --ingest-url "$OBSERVABILITY_INGEST_URL" --credential-env-var OBSERVABILITY_INGEST_CREDENTIAL
  rm -rf "$tmp"
  ```

### 6. Re-apply retention once data flows

Streams exist only after first ingest, so run
`just provision-observability-accounts <instance-ip>` once data is arriving
to land the 90-day log-stream retention overrides (metrics keep the 25-month
instance default).

## Validation gates (run on dev before touching staging)

These are the spec's prototype validation plan as commands. If any pinned
shape is wrong, fix the code (locations below), update the spec, and re-run.

1. **Ingress gate**: `curl -fsS https://telemetry.minds-dev.com/healthz`
   answers 200; `curl -s -o /dev/null -w '%{http_code}'
   https://telemetry.minds-dev.com/web/` answers 404 (no public UI);
   `curl --connect-timeout 5 https://<instance-ip>` times out (origin
   firewall admits only Cloudflare).
2. **UI over SSH only**: `ssh -L 5080:127.0.0.1:5080 debian@<instance-ip>`,
   then sign in at `http://localhost:5080` with the Vault root account.
3. **Direct OTLP push**: POST a minimal OTLP/JSON payload to
   `https://telemetry.minds-dev.com/api/default/v1/logs` with
   `Authorization: <INGEST_CREDENTIAL_BOXES>` and `stream-name: box_logs`;
   expect 200 and the line visible in the UI. This also proves the minted
   sender users' role can ingest (pinned as `service_account` in
   `apps/observability/imbue/observability/openobserve_api.py`, validated on
   the dev v0.92.2 instance -- the OSS release accepts only `admin` and
   `service_account`).
4. **Modal end to end**: after the Modal workspace step (procedure step 4),
   exercise any dev Modal app (e.g. hit the dev connector's
   `/health/liveness`) and confirm `modal_logs` entries plus container
   metrics arrive.
5. **Durability**: `sudo systemctl kill -s SIGKILL openobserve` on the
   instance; it restarts and at most ~the last minute of data is missing.
   Then a full replacement drill per "Replacing an instance" in the spec:
   quiesce the old instance FIRST (stop caddy + openobserve, wait ~2
   minutes), `just provision-observability US-EAST-VA-1 2` (bumped
   ordinal), confirm history survived, then
   `just destroy-observability-instance <old-instance-id>`.
6. **Fleet collectors**: after the collector rollout (procedure step 5) on a
   dev box + relay, confirm the `box_logs` and `relay_logs` streams populate
   and that both hosts' metrics arrive. OpenObserve maps each OTLP metric to
   its own stream (the `stream-name` header only routes logs), so look for
   populated metric streams -- including the box's per-qemu process metrics
   -- rather than a role-named `box_metrics` stream. Query tips (validated
   on dev): journald entries flatten their structured body, so the message
   text is the `body_message` column (not `body`); searching a metrics
   stream through `POST /api/default/_search` needs `?type=metrics` or it
   answers "stream not found".
7. **Retention**: after the retention re-run (procedure step 6), confirm the
   log streams show 90-day retention in the UI (Streams -> settings).

Caddy path matchers live in
`apps/observability/imbue/observability/config_render.py` if the OTLP URL
shapes need adjusting.

## Staging, then production

Repeat the per-tier procedure with `staging` and then `production` activated.
Remember the tiers are fully isolated (own Cloudflare account, Neon org, OVH
credentials, Modal workspace) -- every resource in step 1 is created fresh
per tier. Record completion (and any lessons) in
[next_deploy.md](../next_deploy.md) / [history/](../history/).

## Querying Modal app logs by severity

Modal's OTEL exporter stamps every function-log line `level: INFO` (it does
not parse the line content), so OpenObserve's own `level` column and the UI's
severity filter are meaningless for `modal_logs`. Our Modal apps therefore
emit every line as one JSON object carrying its real level
(`imbue.modal_app_kit.log_format`; LiteLLM's lines use its native JSON
logging with the same `level` field). Query the embedded field instead with
`spath`, OpenObserve's JSON-string extraction function (the path is dot
notation; this is DataFusion SQL, not DuckDB's `json_extract_string` /
`$.path` that the analytics log views use over the parquet export):

```sql
SELECT spath(body, 'level') AS real_level, count(*)
FROM modal_logs GROUP BY real_level

SELECT _timestamp, spath(body, 'logger') AS logger, body
FROM modal_logs
WHERE spath(body, 'level') IN ('WARNING', 'ERROR')
ORDER BY _timestamp DESC
```

Other useful body fields: `type` (`http_request`, `metric`,
`share_visit_authorized`, or `log` for plain text), `minds_env` (the env
that emitted the line on the shared dev instance), `exception` (a folded
traceback). Lines without a `level` are not ours: Modal's own per-request
lines (`GET /account -> 401 Unauthorized (duration: ...)`, `file_descriptor`
3) and the raw traceback Modal's runtime prints, one record per line, when a
function re-raises -- the same failure also arrives as one of our
`level: ERROR` lines with the traceback folded into `exception` (the
connector's 500 handler and `capture_and_reraise` both log it). Our
`imbue.*` loggers emit at INFO and third-party libraries at WARNING; to see
a dev env's DEBUG lines, export the level at deploy time
(`MINDS_LOG_LEVEL=DEBUG uv run minds-admin env deploy`), which the deploy
metadata secret carries into the containers. Lines ingested before the JSON
envelope shipped are plain text and have no `level`. Promoting these fields
to real stream columns would take a real-time pipeline with a VRL
`parse_json` function on `modal_logs`; deliberately not set up until the
dashboards work needs it (mngr-internal#656).

## Ongoing operations

- **Upgrades are replace-not-update**: bump the pinned versions in
  `apps/observability` (`remote_install.py`, `collector_install.py`), then
  run the replacement drill above. Never run two instances against the same
  R2 bucket + metadata database.
- **Credential rotation**: see the rotation note in
  `.minds/template/observability.sh`.
- **Monitoring the monitor**: until the connector health-sweep probe lands
  (follow-up below), a dead instance is only visible as silent dashboards --
  check `https://telemetry.<domain>/healthz` when in doubt.

## Follow-ups after bring-up

- Connector health-sweep probe of `/healthz` (after the Bugsink PR lands).
- A `minds_services` deployment test pushing one log through the public
  ingest path.
- Dashboards + alert-on-no-data rules (separate PR).
- Migrate Bugsink onto this hosting pattern: mngr-internal#464 -- DONE; see
  [bugsink-bringup.md](bugsink.md).
