# Bugsink bring-up (error tracking): dev, staging, production

Operator runbook for standing up the per-tier Bugsink error-tracker
instances and wiring the reporting services to them. Design and rationale
live in `specs/minds-bugsink-error-tracking.md`; the hosting pattern (OVH
VPS + Cloudflare-fronted ingest + SSH-only UI) is shared with the
OpenObserve instances (`observability-bringup.md`), and the CLI surface is
documented in `apps/observability/README.md`. Track the per-tier status in
[next_deploy.md](./next_deploy.md).

Order matters: bring up **dev first** and run the validation gates below --
the ingest path shapes and the tunneled-UI behavior are pinned best-effort
in code and the dev pass is what confirms (or corrects) them. Then staging,
let it soak, then production.

## Prerequisites

- `vault login` (see [vault-setup.md](./vault-setup.md)) and an activated
  env for the target tier (`eval "$(uv run minds-admin env activate <env>)"`).
  Any `dev-*`/`ci-*` env maps to the single shared **dev** instance.
- Access to the tier's Cloudflare account (origin certs + DNS), Neon org,
  and OVH Public Cloud project.

## Per-tier bring-up procedure

Run this once per tier (dev, then staging, then production).

### 1. Create the backing resources

- **Neon**: create a `bugsink` database. For staging/production, inside the
  tier's existing Neon project; for dev, inside a small dedicated project in
  the dev org (mirroring the OpenObserve dev instance). Note the DIRECT
  (non `-pooler`) DSN.
- **Origin certificate**: Cloudflare dashboard -> SSL/TLS -> Origin Server ->
  create a certificate covering `errors.<tier domain>` (the domain is the
  tier's `cloudflare_domain` from its `deploy.toml`, e.g. `minds-dev.com`).
- **SSH keypair**: `ssh-keygen -t ed25519 -f bugsink_key -N ""`.
- **Break-glass account**: the tier operator's own address --
  `josh@imbue.com` (dev), `josh_staging@imbue.com` (staging),
  `josh_production@imbue.com` (production) -- with a strong password
  (`openssl rand -base64 24`), stored as `CREATE_SUPERUSER=email:password`.
- **Django secret key**: >= 50 random chars (the template shows a one-liner).

### 2. Populate Vault

```bash
cp .minds/template/bugsink.sh /tmp/bugsink-<tier>.sh
$EDITOR /tmp/bugsink-<tier>.sh   # fill everything EXCEPT BUGSINK_API_TOKEN (leave empty)
uv run scripts/push_vault_from_file.py <tier> bugsink /tmp/bugsink-<tier>.sh
shred -u /tmp/bugsink-<tier>.sh
```

Also push the tier's `sentry` entry all-empty if it does not exist yet
(`.minds/template/sentry.sh`); provisioning fills the DSNs in. The ci tier
needs its own all-empty `sentry` entry too (provisioning writes the dev
DSNs into both).

### 3. Provision the instance

```bash
eval "$(uv run minds-admin env activate <env>)"   # tier is derived from this
just provision-bugsink <ovh-region>         # e.g. US-WEST-OR-1 for dev
```

Pick the OVH region nearest the tier's Neon project: Bugsink's digest
transaction issues ~70 sequential DB queries per event, so ingest
throughput scales with ~1/RTT (dev Neon is us-west-2 -> `US-WEST-OR-1`).

One shot: orders the OVH VPS, installs the hash-locked Bugsink venv +
caddy ingest gate + nftables firewall, waits for first-boot migrations,
mints a REST API token and get-or-creates the team + per-service projects
(`rsc`, `llm`, and -- dev only -- `oauth-redirector`; DSNs + token written
back to Vault), and points the Cloudflare-proxied `errors.<domain>` record
at the instance. The recipe prints the manual follow-ups when it finishes.

**Note:** re-running provisions a NEW VPS. To re-run only the
token/projects pass against the existing instance, use
`just provision-bugsink-projects <instance-ip>`.

### 4. Re-deploy the tier's reporting services

```bash
uv run minds-admin env deploy --yes-i-mean-<tier>    # staging/production
```

The deploy re-pushes the tier's `sentry` Vault entry as the stamped
`sentry-<tier>-<deploy-id>` Modal Secret, which the connector and LiteLLM
proxy read their DSNs from. Dev/ci envs pick the DSNs up on their next
per-env deploy the same way; the dev oauth-redirector gets its DSN baked at
its next `just deploy-oauth-redirector dev`.

### 5. Sign in (and invite any additional humans)

The break-glass `CREATE_SUPERUSER` account IS the tier operator's own
address, so signing in over the SSH tunnel with the Vault credentials
(`ssh -L 8300:127.0.0.1:8300 debian@<instance-ip>`, then
`http://localhost:8300`) needs no invite step. Additional humans are added
via Bugsink's invite flow; with no email backend configured, the UI
displays the invite link for copy-paste -- no SMTP.

## Validation gates (run on dev before touching staging)

1. **Ingress gate**: `curl -s -o /dev/null -w '%{http_code}'
   https://errors.minds-dev.com/accounts/login/` answers 404 (no public
   login page or UI); a GET of an ingest path, e.g.
   `curl -s -o /dev/null -w '%{http_code}'
   https://errors.minds-dev.com/api/1/envelope/`, answers **405**
   (Django's method-not-allowed; NOT caddy's 404 -- observed on the dev
   bring-up, it proves Django is serving through the gate and is the
   health signal the future connector sweep will probe);
   `curl --connect-timeout 5 https://<instance-ip>` times out (origin
   firewall admits only Cloudflare).
2. **UI over SSH only**: `ssh -L 8300:127.0.0.1:8300 debian@<instance-ip>`,
   then sign in at `http://localhost:8300` with the break-glass account.
3. **Event end to end**: send a test event through a provisioned DSN (e.g.
   `sentry_sdk.init(dsn=<RSC_SENTRY_DSN from Vault>);
   sentry_sdk.capture_message("bugsink bring-up test")` from a
   `uv run python` shell) and confirm it appears in the `rsc` project via
   the tunneled UI. This proves DSN -> Cloudflare -> caddy -> Django ->
   Neon end to end, including eager digestion.
4. **Reporting services**: after step 4's re-deploy, trigger a harmless
   error in a dev env's connector (or wait for CI deployment tests
   exercising failure paths) and confirm the event lands with the right
   `environment` tag and `service` name.
5. **Durability + replacement drill**: `sudo systemctl kill -s SIGKILL
   bugsink` on the instance; it restarts and every previously acked event
   is still there (all state is in Postgres). Then a full replacement:
   stop the bugsink service on the old instance FIRST (single-writer; no
   quiesce window needed -- acked events are already durable), `just
   provision-bugsink <region> 2` (bumped ordinal), confirm history
   survived and DSNs are unchanged, then
   `just destroy-bugsink-instance <old-instance-id>`.

The caddy path matchers live in
`apps/observability/imbue/observability/bugsink_render.py` if the ingest
URL shapes need adjusting (sentry-sdk posts to `/api/<project_id>/envelope/`).

## Staging, then production

Repeat the per-tier procedure with `staging` and then `production`
activated. The tiers are fully isolated (own Cloudflare account, Neon
project, OVH credentials) -- every resource in step 1 is created fresh per
tier. Record completion (and any lessons) in
[next_deploy.md](./next_deploy.md) / [history/](./history/).

## Ongoing operations

- **Upgrades are replace-not-update**: bump the pins in
  `apps/observability/imbue/observability/deploy_assets/bugsink_requirements.in`,
  recompile the hash-locked export (command in that file), re-diff the
  vendored `bugsink_conf.py` against the new upstream `docker.py.template`,
  then run the replacement drill above. Never run two instances against the
  same database.
- **Lifecycle**: the instances are operator-lifecycle, like the relays and
  the OpenObserve instances -- `minds-admin env deploy` / `destroy` never touch
  them, and error history deliberately survives env destroys.
- **Escape hatch**: any `bugsink-manage` subcommand runs over SSH from the
  env file's environment, e.g.
  `ssh debian@<ip> "sudo bash -c 'set -a; . /etc/bugsink/bugsink.env; set +a; cd /opt/bugsink && venv/bin/bugsink-manage vacuum_eventstorage'"`.
- **Kill switch**: `MINDS_SENTRY_DISABLED=1` in a reporting service's
  environment disables its reporting without touching Vault; an empty DSN
  does the same per service.

## Follow-ups after bring-up

- Connector health-sweep probe of the public ingest 4xx signal (shared
  follow-up with the OpenObserve `/healthz` probe).
- A `minds_services` deployment test sending one event through a real dev
  DSN and asserting it appears via the REST API.
- Optionally install the observability collector on the bugsink hosts so
  their host metrics/journals land in the tier's OpenObserve (needs a
  sender-class decision; not part of v1).
