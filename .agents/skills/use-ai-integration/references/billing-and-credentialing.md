# Billing and credentialing model

How each way of calling a model from code is billed and credentialed, and what
it shares with the user's own chats.

## Billing buckets

| How the call is made | What it draws on | Shares the user's chat limits? |
|---|---|---|
| Anthropic API key (`ANTHROPIC_API_KEY`, direct or through a proxy; litellm) | Pay-per-token API account | No |
| `claude -p` on a Claude plan sign-in or long-lived token | The plan's usage limits | Yes |
| `claude -p` on a Console sign-in | Pay-per-token API account of that Console organisation | No |
| Interactive Claude Code / chat | The plan's usage limits | -- |
| OpenAI API key (litellm, or `codex exec` on a pasted key) | Pay-per-token OpenAI API account | No |
| `codex exec` on a ChatGPT sign-in | The ChatGPT plan's usage limits | Yes |
| Interactive Codex chat | The ChatGPT plan's usage limits | -- |

Anthropic's help centre ([Use the Claude Agent SDK with your Claude
plan](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan),
update dated 2026-10-07): `claude -p` and the Agent SDK on a plan sign-in draw
from the plan's usage limits, and the previously announced change to Agent SDK
usage was paused on 2026-06-15. The monthly API credits of Max, Ultra and Team
plans apply only to calls made with an API key from the plan's linked Console
organisation.

So on a subscription, calls from code and the user's chats spend the same
limits: a heavy flow can leave the user unable to chat until the limits reset.
Keep subscription flows small, measure before scaling, and suggest an API key
when the volume is high.

## Whose use a plan sign-in covers

A Claude or ChatGPT plan sign-in is for the account owner's own use. Anthropic's
[legal and compliance page](https://code.claude.com/docs/en/legal-and-compliance)
does not permit routing requests through Free, Pro, Max or Ultra plan
credentials on behalf of other users, and says advertised plan limits assume
ordinary, individual usage. OpenAI's [Codex authentication
page](https://learn.chatgpt.com/docs/auth) recommends API keys for automation
and says not to expose Codex execution in untrusted or public environments. So
an app or automation that other people use (through a sharing link, or as other
people in the workspace) runs on an API-key account.

## Why `claude -p` uses more than the direct API

Not the model -- the **default agent context it reloads per call**: the Claude
Code system prompt, all tool definitions, auto-discovered `CLAUDE.md` / skills,
and a multi-turn tool loop. The completion path (`complete()`, through
`claude_p_completion`) sheds nearly all of it (measured on this repo, Haiku, a
one-line prompt):

| Config | Turns | Context | Notes |
|---|---|---|---|
| Default `claude -p` | ~7 | ~238k | May wander off-task (e.g. tries to commit an unrelated file) |
| `--system-prompt` + `--tools ""` | 1 | ~13k | The residual ~13k is CLAUDE.md + skills |
| above **+ isolated cwd** | 1 | ~0.2k | What `claude_p_completion` does; CLAUDE.md not loaded |
| `--bare` (+ replace) | -- | -- | Strips CLAUDE.md/skills too, but **fails to auth keyless** |

- **`--tools ""` is a correctness fix, not just cost**: the default agent given a
  "just answer this" prompt will use tools and may do unrelated work.
- **The isolated cwd is the real context-bleed fix**: `claude -p` auto-discovers
  `CLAUDE.md` / `.claude` hooks from the *working directory*, so the completion
  path runs from a throwaway temp dir -- with no key and no `--bare` (which can't
  authenticate keyless: it needs `ANTHROPIC_API_KEY` or an `apiKeyHelper`).
  Without it, an empty or weak system prompt lets the ambient `CLAUDE.md` hijack
  the answer (about 1 in 5 trivial-prompt runs). The agentic task path does
  *not* isolate cwd -- it needs the repo context for file access.

`complete()` on a ChatGPT sign-in runs `codex exec` the same way: from an empty
temporary directory, with the account folder's `config.toml` and rules ignored
(`--ignore-user-config`, `--ignore-rules`), a read-only sandbox, and no session
saved (`--ephemeral`), so no `AGENTS.md`, MCP server or hook reaches the answer.
It is still an agent: the read-only sandbox lets it run commands that read any
file the user can, and `codex exec` has no system prompt option, so `system`
only leads the prompt. Untrusted input therefore goes to an Anthropic or
API-key account, where a completion has no tools.

## Credential resolution

Each sign-in through the provider chooser is an account: a folder
`~/.minds/accounts/<id>/` and a row in `~/.minds/accounts/index.json` naming its
provider. The library reads the index on every call and picks the account as
SKILL.md says; it never takes the account from the calling process's
`CLAUDE_CONFIG_DIR` or `CODEX_HOME`.

- **Anthropic.** `read_workspace_ai_credentials(account_dir=...)` resolves the
  key: the `data/.secrets/anthropic.env` snapshot first (with the snapshot's
  base URL, or none), then the account's `settings.json` `env` block; the
  process env is never read for a named account. With a key, `complete()`
  calls litellm with it (and its `ANTHROPIC_BASE_URL`, for a proxy) and
  `run_task()` hands it to `claude -p` through `--settings`, which outranks the
  account's own settings. Without one, `claude -p` runs with
  `CLAUDE_CONFIG_DIR` set to the account folder and authenticates itself from
  it (a `CLAUDE_CODE_OAUTH_TOKEN` in the settings env, or a sign-in in
  `.credentials.json`), so every call uses current auth. The child runs
  without the calling process's `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`,
  `ANTHROPIC_BASE_URL` and `CLAUDE_CODE_OAUTH_TOKEN`: Claude Code copies its
  settings env into the commands it runs, so a script started from a chat
  carries the chat account's credential, which claude would otherwise prefer.
- **Keyed integrations pin their key at setup.** `write_anthropic_env_snapshot()`
  copies `ANTHROPIC_API_KEY` (+ `ANTHROPIC_BASE_URL`) into
  `data/.secrets/anthropic.env` -- a program-mediated copy, which is why the
  secrets guard allows it while refusing a direct read or write of the file --
  so a built service keeps billing that key after the user switches the
  workspace's sign-in. Re-keying is delete and re-snapshot. The subscription
  `CLAUDE_CODE_OAUTH_TOKEN` is never snapshotted: it cannot authenticate direct
  API calls.
- **OpenAI.** The account folder is a `CODEX_HOME`, and its `auth.json` says
  which kind it is. A pasted key (`{"auth_mode": "apikey", "OPENAI_API_KEY":
  "sk-..."}`) is called through litellm by `complete()` and through `codex exec`
  by `run_task()`. A ChatGPT sign-in is only ever used by `codex exec`, with
  `CODEX_HOME` set to the account folder; the library never reads its tokens.
- **Older copies of `claude_p.py`** resolve the account themselves:
  `$CLAUDE_CONFIG_DIR` when set (inside an agent, the chat's account), else the
  workspace's default Anthropic account.
- A call that cannot be made fails with a typed error (`NoUsableAccountError`,
  `UnsupportedProviderError`, `ProviderCallError`, `ProviderTimeoutError`)
  rather than hanging.

## The mngr `claude -p` session-hook bug

mngr sets `MAIN_CLAUDE_SESSION_ID` to mark its managed main session, and its
stop/readiness hooks are guarded on that variable. A child `claude -p` that
inherits it looks like the managed session and trips those hooks. The
`claude_p.py` helper unsets `MAIN_CLAUDE_SESSION_ID` in the child environment,
which neutralizes them. The library does the same for `claude -p`, and runs
`codex exec` without `MNGR_AGENT_STATE_DIR`, so neither child is taken for the
agent that started it -- one reason to call the library rather than shelling out
to either CLI yourself.

## The footguns

If an `ANTHROPIC_API_KEY` is configured (in the settings env block or the
process env), `claude -p` bills **full API rates** against the API account, not
the plan's usage limits. In a key-mode workspace a `claude -p` task path
therefore *is* real per-token spend. An unattended `claude -p` loop on an API
key can run up four-figure spend in days, so surface the projected cost before
scaling a flow.

`CODEX_API_KEY` in a `codex exec` process's environment (not `OPENAI_API_KEY`)
silently switches it to API billing on that key, whatever the account folder
holds. The library removes `CODEX_API_KEY`, `OPENAI_API_KEY` and
`CODEX_ACCESS_TOKEN` from the codex child's environment, so only the account
folder decides how a codex call is billed.
