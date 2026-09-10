# Deploying the tier services

`minds-admin env deploy` does two things per tier: `modal deploy` these apps, and
push the Modal Secrets they read.

| Modal app | serves |
|---|---|
| `rsc-<tier>` | the **remote connector** — sign-in and signup, workspace leasing, sharing, LLM keys, R2 buckets, plans and quotas, the web client, plus cron-like devops tasks such as clearing stale or stuck workspaces |
| `llm-<tier>` | the **LiteLLM proxy** |
| `analytics-<tier>` | analytics, where a tier enables it |

## What the connector depends on

`creates_resources = false` on both tiers: a deploy pushes code and credentials
against infrastructure that already exists. It never provisions any of this.

| | what it is | credential |
|---|---|---|
| **SuperTokens** | auth core behind sign-in, signup, password reset | `supertokens` |
| **Neon** | Postgres, incl. the `pool_hosts` table the lease path reads | `neon` |
| **Cloudflare** | R2 buckets and DNS | `cloudflare` |
| **OVH object storage** | workspace stop/start artifacts | `storage` |
| **the pool SSH key** | injected into a slice at lease time | `pool-ssh` |
| **sharing** | share coordinates, relay list, frps plugin auth, ACME issuance | `sharing` |
| **Sentry DSNs** | point at this tier's self-hosted Bugsink | `sentry` |
| **LiteLLM** | the proxy's own credentials | `litellm` |

Each is a Vault entry at `secrets/minds/<tier>/<service>` matching a schema in
`.minds/template/<service>.sh`; the deploy pushes each as the Modal Secret
`<service>-<tier>`, plus a derived `litellm-connector` secret. A missing key
fails the push before anything ships.

## Deployed separately

Not touched by `env deploy`, and redeployed only when their own code changes:

| | what it is | where |
|---|---|---|
| **share relays** | frps servers; the auth and ingress for **all** sharing, not only local | [below](#share-relays) |
| **Bugsink** | error tracking, one instance per tier — the `sentry` DSNs above point here | [../setup/bugsink.md](../setup/bugsink.md) |
| **OpenObserve** | metrics and logs | [../setup/observability.md](../setup/observability.md) |

A services deploy is **independent of an app release**. The server tracks `main`,
not the release tag, and production routinely runs a commit older than the newest
tag. Deploy for the server changes, or before promoting a channel to beta or
stable.

[../reference/environments.md](../reference/environments.md) owns tiers,
activation, Vault entries and Modal workspaces.
[../setup/tier-bringup.md](../setup/tier-bringup.md) stands one up for the first
time.

## Which commit is running?

`GET /version` reports the live `deploy_id` and tier `generation_id` — not a git
SHA, so recovering the deployed commit means finding that deploy id in a
[history](../history/) entry:

```bash
curl -s https://minds-production--rsc-production-api.modal.run/version
grep -rn "<deploy_id>" ../history/
```

With that SHA, the diff a deploy would ship:

```bash
git log --oneline --no-merges <deployed-sha>..HEAD -- apps/remote_service_connector/imbue apps/analytics/imbue
git diff --name-status <deployed-sha>..HEAD -- apps/remote_service_connector/migrations
git show "<deployed-sha>:apps/minds/imbue/minds/build_info.py" | grep FALLBACK_BRANCH
```

## Is it safe to redeploy?

Yes, and it is the normal way to recover a failed deploy — but it is not a no-op.
Every deploy:

- **Moves the web-create pin** to `FALLBACK_BRANCH` in the tree being shipped.
  `/hosts/claim` (browser creates) matches that tag exactly with no rebuild
  fallback, so deploying ahead of a bake breaks browser creates until the bake
  lands. The desktop falls back to a slow rebuild and is unaffected. Bake first
  ([pool-hosts.md](./pool-hosts.md)), or pass
  `MINDS_WEB_TEMPLATE_REF=<the baked tag>` and re-deploy after.
- **Ships the working tree.** `modal deploy` uploads what is on disk; nothing
  checks a ref. Deploy from a clean tree at the ref you mean to ship.
- **Overwrites the `plans` table** from `deploy.toml`. Per-user entitlement rows
  are untouched.
- **Applies pending migrations**, which flips the strategy to RECREATE — a brief
  cold-boot window — instead of the zero-downtime ROLLOVER.
- **Takes a Neon snapshot**, and on any later failure auto-runs `env recover` on
  a 5-second countdown: the database is restored and every lease, share, signup
  and sync record written during the window is discarded. Ctrl-C within those 5
  seconds to decide deliberately; that only works in the foreground, so a
  backgrounded deploy cannot be interrupted.

A leftover `.minds-deploy-recover-target-*.json` at the repo root, from an
interrupted deploy in any tier, blocks every `minds-admin env` command until
resolved.

## Deploy

```bash
eval "$(uv run minds-admin env activate --deploy production)"
modal profile list        # the token must belong to minds-production
uv run minds-admin env deploy --yes-i-mean-production
```

Activation only checks that a profile of that name exists, not that its token
belongs to that workspace; `modal profile list` is what catches a misroute before
anything ships.

Then confirm it landed:

```bash
curl -s https://minds-production--rsc-production-api.modal.run/version   # deploy_id advanced
curl -s https://minds-production--rsc-production-api.modal.run/health/liveness
```

## Share relays

Redeploy **after** the connector, never before: the old connector fail-closes on
the new plugin form, so relays-first breaks sharing.

```bash
just list-share-relays        # ids, regions, addresses — the arguments below
just services-deploy-share-relay <host> <relay_id> <region> <content_domain> <plugin_auth_url>
```

`plugin_auth_url` is the header form `https://<connector>/frps/auth`. Redeploy
one relay and confirm its live tunnels re-log-in with zero rejects before doing
the rest.
