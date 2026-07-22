# Billing and credentialing model

Why the scenarios are credentialed the way they are, and why a service can call
Claude heavily without blocking the user's interactive chat.

## Three billing buckets

| How the call is made | Bucket it draws | Blocks interactive chat? |
|---|---|---|
| Direct Anthropic API (`ANTHROPIC_API_KEY` set; litellm) | Pay-per-token API account | No |
| `claude -p` on a subscription, no key | Programmatic / Agent-SDK credit pool | No |
| Interactive Claude Code / chat / Cowork | Interactive subscription pool | -- (the pool to protect) |

`claude -p` / Agent-SDK usage draws a separate pool from interactive usage, so
neither service path competes with the user's chat quota. **The live concern is
cost, not chat availability** -- which is why there is no gating to protect the
chat, only cost surfacing.

## Why `claude -p` costs more than the direct API

Not the model -- the **default agent context it reloads per call**: the Claude
Code system prompt, all tool definitions, auto-discovered `CLAUDE.md` / skills,
and a multi-turn tool loop. The keyless completion path in `claude_p.py` sheds
nearly all of it (measured on this repo, Haiku, a one-line prompt):

| Config | Turns | Context | Cost | Notes |
|---|---|---|---|---|
| Default `claude -p` | ~7 | ~238k | ~$0.086 | May wander off-task (e.g. tries to commit an unrelated file) |
| `--system-prompt` + `--tools ""` | 1 | ~13k | ~$0.016 | The residual ~13k is CLAUDE.md + skills |
| above **+ isolated cwd** | 1 | ~0.2k | ~$0.012 | What `claude_p_completion` does; CLAUDE.md not loaded |
| `--bare` (+ replace) | -- | -- | -- | Strips CLAUDE.md/skills too, but **fails to auth keyless** |

- **`--tools ""` is a correctness fix, not just cost**: the default agent given a
  "just answer this" prompt will use tools and may do unrelated work.
- **The isolated cwd is the real context-bleed fix**: `claude -p` auto-discovers
  `CLAUDE.md` / `.claude` hooks from the *working directory*, so the completion
  path runs from a throwaway temp dir -- with no key and no `--bare` (which can't
  authenticate keyless: it needs `ANTHROPIC_API_KEY` or an `apiKeyHelper`). Before
  this, an empty/weak system prompt let the ambient `CLAUDE.md` hijack the answer
  (~1 in 5 trivial-prompt runs). The agentic task path does *not* isolate cwd -- it
  needs the repo context for file access.

## Credential resolution

Workspace credentials live in the `env` block of the shared
`$CLAUDE_CONFIG_DIR/settings.json`, written only by the in-UI Claude sign-in
modal. They are deliberately NOT in the process environment: supervisord
freezes its env at boot, so an env-var credential would go stale the moment
the user changes auth in the modal.

- `read_workspace_ai_credentials()` (in `scripts/claude_p.py`) resolves the
  current settings-env credentials at call time, falling back to the process
  env outside a workspace. `api_key` present -> keyed path (litellm, direct
  API), with `base_url` for proxy (Imbue/LiteLLM) setups.
- Otherwise `claude -p`, which authenticates itself from the same shared
  settings (a `CLAUDE_CODE_OAUTH_TOKEN` there, or a `.credentials.json`
  login) -- every spawn is a fresh claude, so it always uses current auth.
- If neither resolves, the call fails with a clear error from the path it
  attempted (litellm's auth error, or a non-zero `claude -p` exit surfaced as
  `ClaudeCLIError`) rather than hanging.

A service run by supervisord still inherits `CLAUDE_CONFIG_DIR` from the
bootstrap shell, which is what makes the settings file findable.

## The mngr `claude -p` session-hook bug

mngr sets `MAIN_CLAUDE_SESSION_ID` to mark its managed main session, and its
stop/readiness hooks are guarded on that variable. A child `claude -p` that
inherits it looks like the managed session and trips those hooks. The
`claude_p.py` helper unsets `MAIN_CLAUDE_SESSION_ID` in the child environment,
which neutralizes them -- the main reason to use the helper rather than shelling
out to `claude -p` yourself.

## The footgun

If an `ANTHROPIC_API_KEY` is configured (in the settings env block or the
process env), `claude -p` bills **full API rates** against the API account, not
the subscription's programmatic credit. In a key-mode workspace a `claude -p`
task path therefore *is* real per-token spend. An unattended `claude -p` loop on an API key can run up
four-figure spend in days, so surface the projected cost before scaling a flow.
