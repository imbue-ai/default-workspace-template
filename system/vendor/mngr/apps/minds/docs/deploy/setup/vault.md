# Vault Setup

Deploy-time secrets for the minds Modal apps (`remote_service_connector`,
`litellm-proxy`) are stored in **HCP Vault** and pushed to Modal Secrets
by the deploy scripts. This doc describes the Vault layout each tier
expects, plus what every operator needs on their machine.

User-side secrets (`ANTHROPIC_API_KEY`, `GH_TOKEN`, etc.) do **not** go
through Vault; they stay as shell env vars on the operator's machine.

## Prerequisites

- The HCP Vault cluster at
  `vault-cluster-public-vault-df29b16f.9b573ab7.z1.hashicorp.cloud:8200`,
  namespace `admin`, KV v2 mount `secrets/`.
- A local install of the `vault` CLI:
  <https://developer.hashicorp.com/vault/install>
- The operator is responsible for running `vault login` themselves before
  any deploy script; minds never touches the user's Vault token.

```bash
export VAULT_ADDR=https://vault-cluster-public-vault-df29b16f.9b573ab7.z1.hashicorp.cloud:8200
export VAULT_NAMESPACE=admin
vault login -method=oidc   # or whatever your team is set up for
```

## Path layout

Secrets use a **split** layout: each `<service>` is a Vault *directory*, and
every logical key inside it is its own single-field leaf secret holding a
`value`. For example, the `litellm` service for the `ci` tier is laid out as:

```
secrets/minds/ci/litellm/ANTHROPIC_API_KEY   -> { "value": "sk-ant-..." }
secrets/minds/ci/litellm/DATABASE_URL        -> { "value": "postgres://..." }
```

Read a single key with `vault kv get -mount=secrets minds/<tier>/<service>/<KEY>`
(the value is at `.data.data.value`), and list a service's keys with
`vault kv list -mount=secrets minds/<tier>/<service>`. The deploy code and
`push_vault_from_file.py` handle this fan-out for you.

Each tier has two families of Vault entries (each a service directory as
described above):

**Pushed to Modal at deploy time** (the connector + litellm-proxy read
these from their runtime env via `modal.Secret.from_name(...)`):

```
secrets/minds/<tier>/cloudflare
secrets/minds/<tier>/litellm
secrets/minds/<tier>/litellm-connector
secrets/minds/<tier>/neon
secrets/minds/<tier>/pool-ssh
secrets/minds/<tier>/sentry      # error-reporting DSNs; empty until
                                 #   `just provision-bugsink` fills them
                                 #   (see bugsink-bringup.md)
secrets/minds/<tier>/sharing
secrets/minds/<tier>/ssh-ca      # the connector's AppRole for the tier's SSH CA
                                 #   (see "SSH certificate authority" below)
secrets/minds/<tier>/supertokens
```

`pool-ssh` is the gen-1 slice fleet's static management key and goes away
with the last gen-1 box (phase 6 of the gen-1 -> gen-2 cutover); gen-2
boxes authorize no static key at all.

The self-hosted Bugsink error tracker's own config lives in
`secrets/minds/<tier>/bugsink` (every tier except ci; dev holds the SHARED
dev/ci instance's config). Like `observability`, that entry is
operator-only: it is read by the `just provision-bugsink` recipes and never
pushed to Modal. See [bugsink-bringup.md](bugsink.md).

**Read only by `minds-admin env deploy` on a developer's laptop** (never
pushed to Modal -- the connector's runtime doesn't need
create-project permissions):

```
secrets/minds/<tier>/neon-admin   # NEON_API_TOKEN (every tier);
                                  #   NEON_ORG_ID (dev only);
                                  #   NEON_PROJECT_ID (staging / production only)
```

**Sourced manually by the operator** (never pushed to Modal, never read
by `minds-admin env deploy` -- no deployed service or deploy step makes
supplier API calls):

```
secrets/minds/<tier>/ovh          # OVH_APPLICATION_KEY, OVH_APPLICATION_SECRET,
                                  #   OVH_CONSUMER_KEY (shared per-tier bare-metal box
                                  #   supplier credentials, for `minds-admin server`);
                                  #   OVH_CLOUD_PROJECT_ID (the Public Cloud project that
                                  #   share-relay instances are provisioned in)
secrets/minds/<tier>/relay-ssh    # RELAY_SSH_PRIVATE_KEY, RELAY_SSH_PUBLIC_KEY (the tier's
                                  #   share-relay SSH keypair; sourced by the operator for
                                  #   `just provision-share-relay` / `just services-deploy-share-relay`
                                  #   so relays can be redeployed from any machine)
secrets/minds/<tier>/box-storage/<ovh-service-name>
                                  # LUKS_RECOVERY_PASSPHRASE: one leaf per gen-2 box, minted
                                  #   and written by `minds-admin server prep` / `setup`
                                  #   BEFORE the box's storage partition is formatted, read
                                  #   by every re-prep and by `minds-admin server unlock`
                                  #   (see "Storage encryption on a gen-2 box" in
                                  #   host-pool-setup.md). Never written by hand; a lost
                                  #   entry means the box is drained and repaved.
```

The dev-tier `neon-admin` token must have *project-create* scope on
the dev tier's Neon org (not just project-scoped permissions). Every
`minds-admin env deploy` against a dev env creates a brand-new Neon
*project* named `minds-<env>` under `NEON_ORG_ID`, with `host_pool`
and `litellm_cost` databases inside; `minds-admin env destroy` deletes the
project outright.

Staging / production keep a single tier-shared project each, named
by `NEON_PROJECT_ID` in the same Vault entry. The token there only
needs branch-create + restore scope on that project (a project-scoped
token is fine and preferable). `minds-admin env deploy` snapshots the
project's default branch before mutating anything, and `minds-admin env
recover` restores from that snapshot if the deploy fails -- without
`NEON_PROJECT_ID`, the deploy refuses to start because it can't be
rolled back. The actual runtime DSNs for these tiers live in
`secrets/minds/<tier>/neon` and `.../litellm` (the source of truth
for the connector + proxy at runtime).

The `ovh` entry holds the bare-metal box supplier credentials
(currently OVH). These order + manage the bare-metal boxes that Imbue
Cloud slices are carved on. The operator sources them into their shell
when running `minds-admin server ...`; no deployed service or
`minds-admin env deploy` / `destroy` step reads them. Generate the AK/AS/CK
trio at <https://api.us.ovhcloud.com/createApp> for the supplier
endpoint the pool uses (`ovh-us` by default). The shared per-tier
credential is intentionally account-wide so any operator can order +
manage boxes for the tier.

The schema for each `<service>` is the corresponding file under
`.minds/template/<service>.sh` at the repo root. For staging /
production (`creates_resources=false`), `minds-admin env deploy` validates
every key declared by a Modal-pushed template against the Vault entry
before pushing anything to Modal and hard-fails the deploy on a
missing key or an unreadable entry -- shipping a placeholder or
partial secret for a declared service would be a silent outage.
Empty values are allowed (declared-but-unset; skipped at Modal push).
Dev/ci envs keep the bootstrap-friendly behavior: an unpopulated
service becomes a placeholder Modal Secret (logged at error level)
that a later deploy replaces once the entry is filled in.

Note that `vault kv delete` is a *soft* delete: the key stays in the
directory listing with no data. The deploy's reader skips such
tombstones with a warning; to actually remove a key from a service,
use `vault kv metadata delete` so the listing is clean too.

`<tier>` is one of `dev`, `staging`, `production`. Per-dev-env secrets
(the values `minds-admin env deploy` generates per developer for a dev env)
are **not** stored in Vault -- they live on the developer's machine
only in `~/.minds-<name>/secrets.toml` (mode 0600).

## SSH certificate authority (gen-2 management SSH)

Gen-2 slice boxes, the slice VMs on them, and the workspace containers
inside those VMs trust **one SSH certificate authority per tier** for
management access instead of a shared static key
(imbue-ai/mngr-internal#850). The CA lives in Vault's SSH secrets engine,
one mount per tier, and is managed by terraform in the
[imbue-ai/vault](https://github.com/imbue-ai/vault) repo
(`terraform/minds_ssh_ca.tf`):

```
minds-<tier>-ssh/               # one mount per tier: minds-dev-ssh, minds-ci-ssh, minds-staging-ssh, minds-production-ssh
minds-<tier>-ssh/config/ca      # the CA keypair (generated in Vault; the private half never leaves it)
minds-<tier>-ssh/sign/operator  # minds-admin: 12h certificates, principals mngr-operator,mngr-service,mngr-vm,mngr-container
minds-<tier>-ssh/sign/connector # the connector's refresh cron: 8h certificates, mngr-service,mngr-vm,mngr-container
minds-<tier>-ssh/sign/analytics # the same cron, for the analytics collector: mngr-vm,mngr-container
```

Who may sign what is a Vault policy: every employee signs `operator`
certificates on `minds-dev-ssh` and `minds-ci-ssh` (the `employee` policy); the
`minds_staging` / `minds_production` OIDC roles sign on their own tier's
mount; the CI env role (`minds_ci_env_gh`) signs on `minds-ci-ssh`. The connector
holds an **AppRole** per tier (`minds-connector-<tier>`) whose only
capability is `sign/connector` and `sign/analytics` on its own mount --
no kv-v2 secrets.

Hosts pin the CA's *public* key: it is committed as `[ssh_ca] public_key`
in the tier's `deploy.toml` and installed by gen-2 box prep, the slice
carve's cloud-init, and the container setup. Bringing a tier's CA up:

```bash
# 1. In the imbue-ai/vault checkout: terraform apply creates the mount, CA,
#    roles, policies, and the connector AppRole for every tier.
# 2. Read the CA public key and commit it to
#    apps/minds/imbue/minds/config/envs/<tier>/deploy.toml as [ssh_ca] public_key
#    (in the same PR, drop the tier from the pinned
#    test_committed_deploy_tomls_have_no_ssh_ca_until_the_tier_brings_one_up in
#    apps/minds/imbue/minds/config/loader_test.py, which exists so this flip is
#    deliberate):
vault read -field=public_key minds-<tier>-ssh/config/ca
# 3. Mint the connector's AppRole credentials into the tier's ssh-ca entry
#    (the secret-id is deliberately not in terraform state):
vault read -field=role_id auth/approle/role/minds-connector-<tier>/role-id
vault write -f -field=secret_id auth/approle/role/minds-connector-<tier>/secret-id
#    -> VAULT_SSH_APPROLE_ROLE_ID / VAULT_SSH_APPROLE_SECRET_ID via
#       uv run scripts/push_vault_from_file.py <tier> ssh-ca <filled .minds/template/ssh-ca.sh>
# 4. minds-admin env deploy: pushes the ssh-ca Modal Secret and runs the
#    connector's ssh_cert_refresh function once, so the tier's certificate
#    Dict is populated before the first request needs it.
```

Operators never handle a certificate by hand: any `minds-admin` command that
dials a gen-2 box signs (and re-signs when under 6h remain) the key at
`~/.mindsadmin/<tier>/ssh_id` through your own `vault login`, writing the
certificate beside it as `ssh_id-cert.pub`. A raw `ssh -i
~/.mindsadmin/<tier>/ssh_id ...` picks the certificate up automatically.
`MINDS_ADMIN_IDENTITY_DIR` relocates the `~/.mindsadmin` root (for a CI
run or a shared machine that should not keep an operator identity in the
home directory). Revoking a person is removing them from the Vault role's
allowlist: their outstanding certificate expires within 12h and no host
carries anything of theirs to clean up.

Rotating the CA itself (a suspected CA-key compromise) is the one expensive
operation: `vault write -f minds-<tier>-ssh/config/ca` (after deleting the old
one) mints a new keypair, and every gen-2 box, VM, and container on the
tier must be re-prepped / re-baked to pin the new public key, then
`deploy.toml` updated. Rotating the connector's AppRole secret-id is cheap:
mint a new one, update the `ssh-ca` entry, redeploy.

## Populating a tier

For each service, copy the template, fill in the values, and push:

```bash
cp .minds/template/litellm.sh /tmp/dev-litellm.sh
$EDITOR /tmp/dev-litellm.sh
uv run scripts/push_vault_from_file.py dev litellm /tmp/dev-litellm.sh
shred -u /tmp/dev-litellm.sh
```

The helper validates that every key declared by the template is present
in the filled file (empty values are fine -- the deploy step skips them
when pushing to Modal), pushes the entry, and prints a `shred` command
for cleanup.

## Deploying

All deploys (dev / staging / production) flow through the unified
`minds-admin env deploy` CLI on the activated env:

```bash
# Tier deploys (staging / production):
eval "$(uv run minds-admin env activate --deploy staging)"
uv run minds-admin env deploy --yes-i-mean-staging

# Dev env deploys (per-developer):
eval "$(uv run minds-admin env activate --deploy dev-<your-user>)"
uv run minds-admin env deploy
```

(`--deploy` is required: `minds-admin env deploy` refuses without it. See
`docs/environments.md` for the use-vs-deploy split.)

`minds-admin env deploy` reads `apps/minds/imbue/minds/config/envs/<tier>/deploy.toml`
for the Modal workspace name + the list of services to push from
Vault, then runs `modal deploy` for both `llm-<tier>` and
`rsc-<tier>`. Tier deploys write nothing to disk
(the committed in-repo `client.toml` stays the source of truth); dev
env deploys write the resulting URLs to `~/.minds-<name>/client.toml`
and per-env secrets (Neon DSN, SuperTokens connection URI + API key)
to `~/.minds-<name>/secrets.toml` (mode 0600).

The `--yes-i-mean-<tier>` flag is a mandatory safety bar for tier
deploys. `minds-admin env destroy` is dev-env-only and hard-refuses for
`production` / `staging` -- tier teardown is operator-managed outside
this CLI.

## Dynamic dev envs and Vault

`minds-admin env deploy` (when run with a dev env activated) reads a small
set of dev-tier secrets from Vault (the dev-tier Neon API token and the
dev-tier SuperTokens admin key) to
provision per-dev-env resources. The resulting per-dev-env state
(Neon DSN, SuperTokens app id, etc.) is written **only** to
`~/.minds-<name>/secrets.toml` on the developer's machine -- never
back into Vault. Staging / production never write a local
`secrets.toml`; the same values for those tiers live in Vault and are
pushed straight to Modal on each deploy.
