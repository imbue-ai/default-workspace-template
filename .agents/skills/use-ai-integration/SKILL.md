---
name: use-ai-integration
description: Use when writing or reasoning about code that calls an AI model -- an AI-driven app or service, an AI integration, or a skill's scripted [ai-script] step. Covers the library that calls the workspace's signed-in Anthropic or OpenAI account, which account a call uses, what each provider supports from code, the three scenarios (one-shot completion, one-shot agentic task, full agent), and the cost and usage-limit model.
metadata:
  author: imbue
---

# Calling a model from code

This is the shared reference for calling a model from code. Whatever sent you
here -- building an AI-driven app or service, scripting a skill's `[ai-script]`
step, or adding an AI integration elsewhere -- supplies the framing; this skill
is the how.

One library does it: `use_ai_integration_skill.ai`, with `complete()` and
`run_task()`. Each call runs on one of the workspace's signed-in accounts:
through the vendor's own CLI (`claude -p`, `codex exec`) on a sign-in, and
through litellm on an API key. Never hand-roll a litellm, `claude -p` or
`codex exec` call: the library picks the account and its credential, keeps the
child process from being mistaken for the agent that started it, and reports
how the call was billed.

## Contents

- [Pick the scenario](#pick-the-scenario-weakest-that-does-the-job)
- [Which account a call uses](#which-account-a-call-uses)
- [What each provider supports today](#what-each-provider-supports-today)
- [Adding the library](#adding-the-library)
- [Scenario 1: one-shot completion](#scenario-1-one-shot-completion)
- [Scenario 2: one-shot agentic task](#scenario-2-one-shot-agentic-task)
- [Scenario 3: full agent](#scenario-3-full-agent)
- [Pinning an Anthropic API key](#pinning-an-anthropic-api-key)
- [Cost and usage limits](#cost-and-usage-limits)

## Pick the scenario (weakest that does the job)

The call falls into one of three scenarios, by how much agency the model
needs. Pick the weakest -- it is cheaper, faster, and simpler.

1. **One-shot completion** (`complete()`) -- no agency: classify, summarize,
   extract, rewrite, answer-from-context. One prompt, one response, no tools.
   The common case.
2. **One-shot agentic task** (`run_task()`) -- a single self-contained job that
   needs tools or file access ("read this file and act", "summarize the diff
   with the repo open"). On an Anthropic account this is also how you **search
   the web**.
3. **Full agent** -- a full, possibly long-running agent that runs in its **own
   git worktree** (a `launch-task` worker). Reach for this over scenario 2 when
   the model edits code that must be tested and validated, or when several
   agents work in the same repo and their changes must not collide. **User- or
   error-triggered only, never an autonomous loop**, with a tightly-scoped task.

## Which account a call uses

With no `account` argument, a call runs on the workspace's default account
among its Anthropic and OpenAI ones: the pinned default, else the most recently
used, else the oldest. Accounts of other providers are skipped. The current
chat's own account is not consulted, so a call made from inside a chat can run
on a different account than the chat does. To use a specific account, pass its
id: `complete(..., account="<id>")`. Accounts are read on every call, so a new
sign-in takes effect without a restart. List them, with the default marked:

```bash
uv run --no-sync python -c "from use_ai_integration_skill.ai_accounts import accounts_root, list_ai_accounts; [print(row) for row in list_ai_accounts(accounts_root())]"
```

## What each provider supports today

| Signed in with | `complete()` | `run_task()` | Billed as |
|---|---|---|---|
| Anthropic: Claude plan, or a long-lived token | `claude -p` | `claude -p` | the plan's usage limits |
| Anthropic: API key (also through a proxy) | litellm | `claude -p` on the key | per token |
| Anthropic: Console sign-in | `claude -p` | `claude -p` | per token |
| OpenAI: ChatGPT plan | `codex exec` | `codex exec` | the plan's usage limits |
| OpenAI: API key | litellm | `codex exec` on the key | per token |
| Google sign-in | never | never | -- |
| Opencode Go, OpenRouter, other API keys | not yet | not yet | -- |

A Google sign-in can never be used from an app or a script: Google's terms for
its consumer sign-in do not allow wrapping its CLI in apps. Calling Gemini would
need a Gemini API key, which is not supported yet. Naming an account of an
unsupported provider raises `UnsupportedProviderError`, which names the
provider. When the user has no Anthropic or OpenAI account, tell them in plain
words which sign-in the feature needs.

## Adding the library

- **A skill**: add `"use-ai-integration-skill"` to the dependencies in its
  `python/pyproject.toml`, run `uv lock` and then `uv sync --all-packages` at the
  repo root, and import `use_ai_integration_skill.ai` inside the subcommand
  function that calls the model: importing it loads pydantic, which an entry
  point may not load at import (`.agents/shared/references/running-python.md`).
- **An app or service** (its own uv project under `system/apps/<name>/` or
  `system/services/<name>/`): from that directory run

  ```bash
  uv add --editable ../../../.agents/skills/use-ai-integration/python
  ```

  and restart it the usual way; its program syncs its own venv at start.

Services that hold a copy of `claude_p.py` keep working, but new code imports
the library instead of copying the file.

## Scenario 1: one-shot completion

```python
from use_ai_integration_skill.ai import complete

CLASSIFY_MODEL = None  # None runs the provider's default; a model id ties the call to one provider

result = complete(
    "Classify this email's intent:\n\n" + email_body,
    system="You are an email triage classifier.",
    model=CLASSIFY_MODEL,
)
print(result.text, result.billing, result.usage)
```

- **No tools.** A step that needs one, web search included, is scenario 2, not
  a provider-side tool bolted onto a completion.
- **The model is set in the code**: a top-of-file constant plus a `--model`
  override, independent of the chat's `/model`. Pass a model id only when you
  know which provider the account is on; with `None`, an API key runs the small
  `DEFAULT_ANTHROPIC_MODEL` or `DEFAULT_OPENAI_API_MODEL`, an Anthropic sign-in
  runs `DEFAULT_ANTHROPIC_MODEL`, and a ChatGPT sign-in runs its account's
  default model (`result.model` is then `None`).
- **Calls are synchronous.** Once a few items give good results, run a batch on
  a `concurrent.futures.ThreadPoolExecutor` with enough workers to saturate the
  I/O (well into the dozens), backing off only on rate limits.
- **Structured output**: ask for JSON matching a pydantic model, validate it,
  and on a validation error retry once with the error appended to the prompt.
- **Errors** (`use_ai_integration_skill.errors`): `AICallError` and its
  subclasses `NoUsableAccountError`, `UnsupportedProviderError`,
  `ProviderCallError` and `ProviderTimeoutError`. Let them fail the step loudly.

## Scenario 2: one-shot agentic task

```python
from use_ai_integration_skill.ai import run_task

result = run_task(
    "Read data/.apps/email-triage/latest.json and draft a reply using templates/.",
    instructions="Only touch files under data/.apps/email-triage/.",
    cwd="/home/user/workspace",
)
```

It runs `claude -p` with permission prompts bypassed, or `codex exec` without
codex's own sandbox, in `cwd` (by default the current directory) with file and
shell access, so scope `instructions` tightly. `instructions` layer on the
CLI's default agent rather than replace it. On an Anthropic account the agent
can search the web. Per-call overhead dominates, so **batch** items into fewer,
larger calls rather than one call per item.

## Scenario 3: full agent

Reach for this over scenario 2 when the work needs its **own git worktree**:
the model is editing code that has to be tested and validated, or other agents
are working in the same repo and the changes must not collide. A `launch-task`
worker gives the run an isolated branch and worktree; scenario 2 instead runs in
the caller's own working directory.

Launch the worker synchronously and collect its structured result -- do not wrap
it; call the script directly:

```bash
uv run --no-sync .agents/skills/launch-task/scripts/create_worker.py launch-sync \
  --name email-triage-fix-123 --template worker \
  --runtime-dir data/.apps/email-triage/fix-123 \
  --task-file  data/.apps/email-triage/fix-123/task.md \
  --timeout 30m --result-json data/.apps/email-triage/fix-123/result.json
```

It launches, waits for the worker's finish report in the foreground, writes a JSON
result (`timed_out`, `type`, `name`, `body`, `branch`, `raw_report`,
`destroy_failed`) to `--result-json`, and destroys the worker and any
sub-workers it launched (the `mngr/<name>` branch survives; a destroy that
left something behind sets `destroy_failed` and exits non-zero, with the
result still written).
Write the task file first with `finish_report_path` frontmatter
(see the `launch-task` skill). **User- or error-triggered, tightly scoped** -- a
broad unattended launch is how cost and time run away. What to do with the
returned branch (merge, review) is your concern.

## Pinning an Anthropic API key

When the workspace's Anthropic account uses an API key, an integration pins that
key at setup so it keeps billing the same key after the user switches sign-in.
Check whether there is a key:

```bash
uv run --no-sync python -c "from use_ai_integration_skill.claude_p import read_workspace_ai_credentials; print('keyed' if read_workspace_ai_credentials().api_key else 'keyless')"
```

When it says `keyed`, snapshot the key (and the proxy base URL that goes with
it) into `data/.secrets/anthropic.env` once, while setting up:

```bash
uv run --no-sync python -c "from use_ai_integration_skill.claude_p import write_anthropic_env_snapshot; print(write_anthropic_env_snapshot())"
```

- Only the key and base URL go in the snapshot, never
  `CLAUDE_CODE_OAUTH_TOKEN` (a subscription token cannot authenticate direct API
  calls, and the writer refuses it).
- The snapshot pins **every** Anthropic call from code to that key, whichever
  Anthropic account is named; `result.account_id` still names that account.
- To retire an integration, delete `data/.secrets/anthropic.env` when the user
  asks; to re-key it, delete the file and run the snapshot writer again.
- Never edit the file: the snapshot writer is the one program-mediated copy the
  secrets guard allows, and the guard refuses direct reads and writes of
  `data/.secrets/` (see the `connect-external-service` skill).

## Cost and usage limits

Read `result.billing`:

- `"subscription"`: the call drew on the plan's usage limits, the same limits
  the user's own chats use. `result.cost_usd` is `None`; never quote a dollar
  figure for it. Describe its volume in calls and tokens (`result.usage`).
- `"api_key"`: billed per token, and `result.cost_usd` is the dollars (`None`
  when litellm has no price for the model, and for `codex exec` on a key, which
  reports none).

**Measure before scaling, don't guess.** Run **one real unit** of the work, read
its usage (and its cost, on a key) and wall-clock, and extrapolate to the full
run (`N x per-unit`, plus retries, divided by your pool size for wall-clock).
Tell the user before turning on a volume flow: on a subscription, in calls and
the share of the plan's limits it may use; on a key, in dollars. To show what a
key would charge for a subscription flow, reprice `result.usage` with litellm's
`cost_per_token` -- an estimate of a key's price, not a saving.

See [references/billing-and-credentialing.md](references/billing-and-credentialing.md)
for the billing buckets, why `claude -p` reloads so much context, how
credentials are resolved, and the two footguns (a stray `ANTHROPIC_API_KEY` or
`CODEX_API_KEY` switches a CLI to API billing).
