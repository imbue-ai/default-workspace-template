# LiteLLM Proxy on Modal

A serverless [LiteLLM](https://github.com/BerriAI/litellm) proxy deployed as a Modal ASGI function. Provides cost tracking via virtual keys for all Claude API usage routed through it.

## Architecture

- **Modal function** (`app.py`): Deployed by file path; ships only this file plus the shared `imbue.modal_app_kit` deploy conventions (no other monorepo imports allowed -- see [libs/modal_app_kit/README.md](../../libs/modal_app_kit/README.md)). Uses `@modal.asgi_app()` to serve LiteLLM's FastAPI app as a long-lived serverless function.
- **Database**: Neon PostgreSQL for cost tracking, key management, and spend logs.
- **Auth**: LiteLLM master key for admin operations; virtual keys for per-user/per-agent cost tracking.
- **Anthropic SDK compatible**: LiteLLM's native `POST /v1/messages` route accepts the Anthropic API request shape with a virtual key (`x-api-key` or `Authorization: Bearer sk-...`). Setting `ANTHROPIC_BASE_URL` to the proxy URL (no path suffix) routes the Anthropic SDK / Claude Code through the proxy with full cost tracking.

## Setup

### 1. Deploy (pushes secrets + runs `modal deploy`)

```bash
eval "$(uv run minds-admin env activate production)"
uv run minds-admin env deploy --yes-i-mean-production
```

`minds-admin env deploy` reads `apps/minds/imbue/minds/config/envs/production/deploy.toml`
for the Modal workspace + the list of services to push from Vault,
creates the `litellm-production` Modal secret with:

- `ANTHROPIC_API_KEY` -- for forwarding to Anthropic
- `DATABASE_URL` -- Neon PostgreSQL connection string
- `LITELLM_MASTER_KEY` -- admin API key

and then runs `uv run modal deploy apps/modal_litellm/app.py` with
`MNGR_DEPLOY_ENV=production`. The `--yes-i-mean-production` flag is
the mandatory safety bar; substitute `--yes-i-mean-staging` (and
`activate staging`) for the staging tier.

### 3. First-time DB migration

On the first cold start, LiteLLM runs ~118 Prisma migrations against the database. This takes ~14 minutes. Subsequent container starts take ~6 seconds.

The `min_containers` setting keeps containers warm to avoid cold
starts. ``minds-admin env deploy`` reads the value from the tier's
``apps/minds/imbue/minds/config/envs/<tier>/deploy.toml``
(``[min_containers].litellm_proxy``, default ``0``; staging and
production ship with ``1``) and threads it into ``modal deploy`` as
``MINDS_LITELLM_PROXY_MIN_CONTAINERS``. The value is read at module
load, which is when ``modal deploy`` serializes the function spec.
To override for a one-off deploy you control directly, export the env
var before running ``modal deploy`` by hand.

### 4. Create a virtual key

```bash
PROXY_URL="https://<workspace>--llm-production-proxy.modal.run"

curl -s -X POST "$PROXY_URL/key/generate" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"key_alias": "my-agent"}'
```

### 5. Use with Claude Code

```bash
export ANTHROPIC_BASE_URL="https://<workspace>--llm-production-proxy.modal.run/"
export ANTHROPIC_API_KEY="sk-your-virtual-key"

claude -p "hello"
```

## Local development

For local testing without Modal, use the `litellm_proxy/` directory at the repo root:

```bash
# One-time setup
uv tool install "litellm[proxy]" --with prisma

# Generate prisma client (one-time)
DATABASE_URL="..." ~/.local/share/uv/tools/litellm/bin/prisma generate \
  --schema ~/.local/share/uv/tools/litellm/lib/python3.12/site-packages/litellm/proxy/schema.prisma

# Start the proxy
./litellm_proxy/start.sh
```

See `litellm_proxy/start.sh` output for virtual key creation instructions.

## Supported models

Every Claude model, automatically. The proxy registers a single pattern entry --
`model_name: "claude-*"` forwarding to `anthropic/claude-*` -- so a client's bare
model name (`claude-opus-5`) is routed upstream as `anthropic/claude-opus-5`
without an entry per model. A newly released Claude model is routable the day it
ships, with no config change and no deploy.

The pattern is scoped to `claude-*` rather than a bare `*` on purpose: this proxy
carries only an Anthropic credential, so a non-Claude model name (`gpt-5.2-codex`,
or a typo) returns an unknown-model error here instead of being forwarded to
Anthropic and coming back as a confusing upstream failure.

Pricing is not pinned here. litellm fetches its
`model_prices_and_context_window` map remotely at startup
(`LITELLM_LOCAL_MODEL_COST_MAP` is deliberately unset), and that map is what the
proxy bills from. It carries dimensions a single inline per-token price cannot
express:

- the fast-mode premium (`provider_specific_entry.fast` -- 2x on Opus 5 and Opus 4.8)
- the regional-processing uplift (`provider_specific_entry.<region>`)
- the 1-hour cache-write rate (`cache_creation_input_token_cost_above_1hr`, 2x
  base, against the 1.25x 5-minute rate a lone inline field assumes)

If litellm's map ever lags a brand-new model, the fix is a cost-map reload
(`POST /reload/model_cost_map` as proxy admin) rather than a code change.

`mngr_usage` keeps its own copy of these prices, because it runs on agent
machines that never import litellm; `litellm_pricing_test` pins that copy against
this same map so the two cannot drift.

## Log lines

Every line the deployed proxy emits is one JSON object carrying an explicit
`level`, so severity queries over the tier's OpenObserve `modal_logs` stream
use `spath(body, 'level')` (Modal's OTEL exporter itself
stamps every line `INFO`). Two mechanisms cover the two halves of the
container: the shared `imbue.modal_app_kit.log_format.configure_logging`
bootstrap for our own lines (the access-log middleware, `migrate_db`), and
LiteLLM's native JSON logging for its own -- `litellm_settings.json_logs`
in the deployed config plus the `JSON_LOGS=1` / `LITELLM_LOG=INFO` env vars
both Modal functions (`litellm_app()`, `migrate_db()`) export before importing
LiteLLM. Once the config loads,
LiteLLM's JSON handler also takes over the root logger, so our plain lines
in the proxy container render through it from then on (still with `level`).
`LITELLM_LOG` is the level LiteLLM's own loggers emit at: INFO matches our
`imbue.*` packages (unset, they would inherit the root logger's WARNING);
set `LITELLM_LOG=DEBUG` in the `litellm` secret of a dev env to get
LiteLLM's debug output. The local-dev `litellm_proxy/config.yaml` keeps
LiteLLM's human-readable text format.

## Checking spend

```bash
curl -s "$PROXY_URL/key/info?key=sk-your-virtual-key" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" | python3 -m json.tool
```

The `spend` field shows cumulative USD spend for that key.

## Troubleshooting

### ModuleNotFoundError for litellm modules

**Cause**: `uv run` syncs from `pyproject.toml` and strips litellm (not a project dependency) from the venv.

**Fix**: Use `uv tool install "litellm[proxy]"` for local development, or deploy on Modal where the image has litellm installed properly.

### Database URL empty / litellm can't connect

**Cause**: Unquoted URLs containing `&` in `.env` files -- bash interprets `&` as a background operator.

**Fix**: Quote all URLs: `export DATABASE_URL='postgresql://...?sslmode=require&channel_binding=require'`

### Port randomization

LiteLLM randomizes the port if the default (4000) is in use. Kill stale litellm processes before restarting.
