# Billing and credentialing model

Why the patterns are credentialed the way they are, and why a service can call
Claude heavily without ever blocking the user's interactive chat.

## Three billing buckets

| How the call is made | Bucket it draws | Blocks interactive chat? |
|---|---|---|
| Direct Anthropic API (`ANTHROPIC_API_KEY` set) | Pay-per-token API account (separate contract) | No |
| `claude -p` on a subscription, no key | Programmatic / Agent-SDK credit pool (finite, then full API rates) | No |
| Interactive Claude Code / chat / Cowork | Interactive subscription pool | -- (this is the pool to protect) |

As of the **2026-06-15 subscription split**, `claude -p` / Agent-SDK usage draws
a *separate* pool from interactive usage. So neither the direct API nor `claude -p`
competes with the user's chat quota. (Before that cutover they shared a pool; the
library's design targets the post-cutover model.)

Consequence: **the live concern is cost, not chat availability.** That is why the
library logs the billing path and supports a spend ceiling, rather than gating
calls to protect the chat.

## The footgun

If `ANTHROPIC_API_KEY` is set in the environment, `claude -p` bills **full API
rates** against the API account, not the subscription's programmatic credit. In a
deployed mngr agent the key is typically forwarded (via `.mngr/settings.toml`), so
`run_completion` will usually take the direct-API path -- which is what you want
(cheapest for non-agentic work), but it *is* real per-token spend. Surface the
projected cost to the user before scaling a flow up. (A real incident ran ~$1,800
in two days from an unattended `claude -p` loop on an API key.)

## Credential resolution (what the library checks)

`run_completion` routes by key presence; all paths require *some* credential:

1. `ANTHROPIC_API_KEY` in the environment -> direct API.
2. Otherwise `claude -p`, which authenticates from the inherited
   `CLAUDE_CONFIG_DIR` (or `~/.claude`) -- `.credentials.json` (OAuth) or
   `~/.claude.json`'s `primaryApiKey`.
3. If neither resolves, the library raises `CredentialsUnavailableError` with a
   clear message rather than letting `claude` fail opaquely.

A service started from `services.toml` inherits the agent's environment (the
bootstrap manager's tmux default-command sources the host + agent env files), so
in a deployed agent both `CLAUDE_CONFIG_DIR` and (usually) `ANTHROPIC_API_KEY` are
present and `claude -p` "just works".

## The mngr `claude -p` session-hook bug

mngr sets `MAIN_CLAUDE_SESSION_ID` in an agent's environment to mark its managed
main session. Every mngr stop/readiness hook is guarded on that variable
(`[ -z "$MAIN_CLAUDE_SESSION_ID" ] && exit 0`). If a child `claude -p` inherits
the variable, it looks like the managed main session and engages mngr's hook
machinery -- the failure mode you hit when calling `claude -p` directly.

The library builds the `claude -p` child environment with `MAIN_CLAUDE_SESSION_ID`
**unset**, which neutralizes all those hooks (confirmed sufficient; the other
`MNGR_*` vars are not load-bearing for this bug, though `build_claude_cli_env`
can strip them too as defense-in-depth). This is why services should always go
through the library rather than spawning `claude -p` themselves.
