# Billing and credentialing model

Why the patterns are credentialed the way they are, and why a service can call
Claude heavily without blocking the user's interactive chat.

## Three billing buckets

| How the call is made | Bucket it draws | Blocks interactive chat? |
|---|---|---|
| Direct Anthropic API (`ANTHROPIC_API_KEY` set) | Pay-per-token API account | No |
| `claude -p` on a subscription, no key | Programmatic / Agent-SDK credit pool | No |
| Interactive Claude Code / chat / Cowork | Interactive subscription pool | -- (the pool to protect) |

As of the **2026-06-15 subscription split**, `claude -p` / Agent-SDK usage draws a
separate pool from interactive usage. So neither path competes with the user's
chat quota, and **the live concern is cost, not chat availability** -- which is why
the library logs the billing path and supports a spend ceiling rather than gating
calls to protect the chat.

## The spend ceiling (optional, `services.toml`-driven)

Spend tracking is opt-in and configured in `services.toml`, not in code -- the
`run_*` functions take no tracker. The library resolves the ceiling from
`[services.<service_name>.ai_spend]`:

```toml
[services.email-triage.ai_spend]
ceiling_usd = 5.0          # rolling-window budget
window_seconds = 86400     # optional; default 24h
```

Each call then checks the ceiling before spending and records the cost after.
Spend is aggregated per service across every call via the persisted ledger at
`runtime/<service_name>/ai_spend.json`, so it survives restarts. Once the window's
spend reaches the ceiling, the next call raises `SpendCeilingExceededError` and
logs instead of spending silently (catch it to route a notice through
`send-user-message`). No table -> unbounded.

The `ai_spend` table is independent of `command`: a service that needs a budget
but isn't a running background process can declare it with no `command` -- the
bootstrap manager skips command-less entries, while the spend loader still finds
the budget by name.

## Why `claude -p` costs more than the direct API

Not the model -- the **default agent context it reloads per call**: the Claude Code
system prompt, all tool definitions, auto-discovered CLAUDE.md / skills, and a
multi-turn tool loop. Three levers control this (on this repo, Haiku, a one-line
prompt):

| Config | Turns | Context | Cost | Notes |
|---|---|---|---|---|
| Default `claude -p` | ~7 | ~238k | ~$0.086 | May wander off-task (e.g. try to commit an unrelated file) |
| `--system-prompt <s>` + `--tools ""` | 1 | ~13k | ~$0.016 | What `run_completion` uses; ~13k is CLAUDE.md + skills |
| above **+ isolated cwd** | 1 | ~0.2k | ~$0.012 | `run_completion`'s keyless fallback; CLAUDE.md not loaded |
| `--bare` (+ replace) | -- | -- | -- | Strips CLAUDE.md/skills too, but **fails to auth keyless** |

- **`--tools ""` is a correctness fix, not just cost**: the default agent given a
  "just answer this" prompt will use tools and may do unrelated work. The
  `run_completion` fallback always disables tools.
- **`run_completion` runs the keyless CLI from a throwaway cwd** so the
  auto-discovered CLAUDE.md / `.claude` hooks don't load (or hijack the answer);
  this drops the residual ~13k to ~0 with no key and no `--bare` (which can't
  authenticate keyless: it needs `ANTHROPIC_API_KEY` or an `apiKeyHelper`).
  `run_task` does *not* isolate cwd -- it needs the repo context. The library never
  uses bare.
- **The savings nudge is honest**: `result.cost_usd` on the fallback already
  reflects the stripped config, so "set a key to save ~$Z" compares the stripped
  `claude -p` cost against the direct-API counterfactual.

Other CLI flags the library can use: `--system-prompt-file` /
`--append-system-prompt-file`, `--json-schema` (structured output), and
`--max-budget-usd` (per-invocation hard cap, complementary to the cross-call
ceiling).

## The footgun

If `ANTHROPIC_API_KEY` is set, `claude -p` bills **full API rates** against the API
account, not the subscription's programmatic credit. In a deployed mngr agent the
key is typically forwarded, so `run_completion` usually takes the direct-API path
-- what you want, but it *is* real per-token spend. An unattended `claude -p` loop
on an API key can run up four-figure spend in days, so surface the projected cost
before scaling a flow.

## Credential resolution

`run_completion` routes by key presence; all paths require *some* credential:

1. `ANTHROPIC_API_KEY` in the environment -> direct API.
2. Otherwise `claude -p`, authenticating from the inherited `CLAUDE_CONFIG_DIR`
   (or `~/.claude`).
3. If neither resolves, the library raises `CredentialsUnavailableError` with a
   clear message rather than letting `claude` fail opaquely.

A service started from `services.toml` inherits the agent's environment, so in a
deployed agent both `CLAUDE_CONFIG_DIR` and (usually) `ANTHROPIC_API_KEY` are
present and `claude -p` just works.

## The mngr `claude -p` session-hook bug

mngr sets `MAIN_CLAUDE_SESSION_ID` to mark its managed main session, and its
stop/readiness hooks are guarded on that variable. A child `claude -p` that
inherits it looks like the managed session and trips those hooks -- the failure you
hit when shelling out to `claude -p` directly. The library builds the child
environment with `MAIN_CLAUDE_SESSION_ID` unset, which neutralizes them. This is
why services should always go through the library rather than spawning `claude -p`
themselves.
