---
name: use-ai-integration
description: Use when writing or reasoning about code that calls an AI model -- an AI-driven app or service, an AI integration, or a skill's scripted [ai-script] step. Covers the library that calls any of the workspace's signed-in AI accounts (Anthropic, OpenAI, Google, Opencode Go, OpenRouter, API keys), which account a call uses, what each account type supports from code, the three scenarios (one-shot completion, one-shot agentic task, full agent), and the cost and usage-limit model.
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
through the vendor's own CLI (`claude -p`, `codex exec`, `agy`, `pi`) on a
sign-in or key, and through litellm on an Anthropic or OpenAI API key. Never
hand-roll a litellm or CLI call: the library picks the account and its
credential, keeps the child process from being mistaken for the agent that
started it, and reports how the call was billed.

## Contents

- [Pick the scenario](#pick-the-scenario-weakest-that-does-the-job)
- [Which account a call uses](#which-account-a-call-uses)
- [What each account type supports](#what-each-account-type-supports)
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
among all its accounts: the pinned default, else the most recently used, else
the oldest. Neither the current chat's own account nor a credential in the
calling process's environment is consulted, so a call made from inside a chat
can run on a different account than the chat does. To use a specific account,
pass its id: `complete(..., account="<id>")`. Accounts are read on every
call, and with none pinned the default follows the user's latest chat. List
them, with the default and each one's billing:

```bash
uv run --no-sync python -c "from use_ai_integration_skill.ai import list_accounts; [print(row) for row in list_accounts()]"
```

**A plan sign-in is for its owner's own use.** An app or automation that other
people use (through a sharing link, or as other people in this workspace) runs
on an API-key account -- one whose listing shows `billing` `api_key`, and not a
coding or token plan's key -- named with `account=`: Anthropic does not allow
routing other people's requests through plan credentials, OpenAI recommends API
keys for automation, and an Opencode Go key is a subscription. Google's rule is
stricter (below).

## What each account type supports

| Signed in with | `complete()` | `run_task()` | Cost reported | Billed as |
|---|---|---|---|---|
| Anthropic: Claude plan or long-lived token | `claude -p`, no tools | `claude -p` | -- | the plan's usage limits |
| Anthropic: API key (also through a proxy) | litellm, no tools | `claude -p` on the key | yes | per token |
| Anthropic: Console sign-in | `claude -p`, no tools | `claude -p` | yes | per token |
| OpenAI: ChatGPT plan | `codex exec`, an agent | `codex exec` | -- | the plan's usage limits |
| OpenAI: API key | litellm, no tools | `codex exec` on the key | `complete()` only | per token |
| Google sign-in | `agy`, an agent | `agy` | -- | the plan's usage limits |
| Opencode Go | `pi`, no tools | `pi` | -- | the subscription |
| OpenRouter, or a key added through the API key option | `pi`, no tools | `pi` | yes | per token |

Caveats: a Google sign-in on a Cloud project's Pay As You Go bills per token,
reported as `subscription`; Opencode Go spends its pay-as-you-go balance once
Extra Usage is on, and is meant for coding-agent traffic; a coding or token plan
key (ZAI, Kimi For Coding, Qwen, Xiaomi) spends the plan's quota, and its
`cost_usd` is a list price.

On a ChatGPT or Google sign-in a completion is an agent turn that can still
read files (and, on Google, fetch web pages), so untrusted text (emails, web
pages, other people's messages) goes to an account whose `complete()` has no
tools -- Anthropic, a `pi` account or an OpenAI key -- named with `account=`.

Google's [FAQ](https://antigravity.google/docs/faq) calls wrapping `agy` in any
third-party app or coding agent unsupported on consumer plans, and its
[Terms](https://antigravity.google/terms) (clause 6) call third-party access a
breach that may get the Google account suspended or terminated. The library
still runs `agy` on a Google sign-in, as chats do: keep that for the owner's own
scripting, and have any app or automation call Gemini on a Gemini API key (the
API key option's Google Gemini provider).

Model ids, flags, and what each CLI leaves in the account folder:
[references/providers.md](references/providers.md). An account of a type the
library does not know raises `UnsupportedProviderError`.

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

CLASSIFY_ACCOUNT = "<id>"  # tool-less (Anthropic, a pi account, an OpenAI key): the email is untrusted
CLASSIFY_MODEL = None  # None runs the account's default model

result = complete(
    "Classify this email's intent:\n\n" + email_body,
    system="You are an email triage classifier.",
    model=CLASSIFY_MODEL,
    account=CLASSIFY_ACCOUNT,
)
print(result.text, result.billing, result.usage)
```

- **Tools**: none on Anthropic, a `pi` account or an API key; an agent on a
  ChatGPT or Google sign-in, where `system` only leads the prompt. A step that
  needs a tool, web search included, is scenario 2.
- **One prompt, no streaming**: put a conversation's earlier turns in the
  prompt text; the answer arrives whole.
- **The model is set in the code**: a top-of-file constant plus a `--model`
  override, independent of the chat's `/model`. Code that sets a model id also
  passes `account=`, so the provider cannot change under it. Each account
  type's default for `None` is in providers.md.
- **Calls are synchronous.** Once a few items give good results, run a batch on
  a `concurrent.futures.ThreadPoolExecutor`: dozens of workers on an API key
  through litellm, but a few on a CLI account (`claude -p`, `codex`, `agy`,
  `pi`), where each worker is a whole agent process; measure, and back off on
  rate limits.
- **Structured output**: ask for JSON matching a pydantic model, validate it,
  and on a validation error retry once with the error appended to the prompt.
- **Errors** (`use_ai_integration_skill.errors`): `AICallError` and its
  subclasses `NoUsableAccountError`, `UnsupportedProviderError`,
  `EmptySystemPromptError`, `ModelRequiredError` (a `pi` provider with no
  default model), `ProviderCallError` and `ProviderTimeoutError`. Let them fail
  the step loudly.

## Scenario 2: one-shot agentic task

```python
from use_ai_integration_skill.ai import run_task

result = run_task(
    "Read data/.apps/email-triage/latest.json and draft a reply using templates/.",
    instructions="Only touch files under data/.apps/email-triage/.",
    cwd="/home/user/workspace",
)
```

It runs `claude -p` with permission prompts bypassed, `codex exec` without
codex's own sandbox, `agy` with permissions skipped, or `pi` (which has no
approval step), in `cwd` (by default the current directory) with file and
shell access, so scope `instructions` tightly and never give it untrusted
input. `instructions` layer on the CLI's default agent rather than replace it.
On an Anthropic account the agent can search the web; on any other account,
fetch the pages in code and pass their text to `complete()`. Per-call overhead
dominates, so **batch** items into fewer, larger calls rather than one call per
item.

## Scenario 3: full agent

A `launch-task` worker gives the run its own branch and worktree; scenario 2
runs in the caller's own working directory. Launch the worker synchronously and collect its structured result -- do not wrap
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
broad unattended launch is how cost and time run away.

## Pinning an Anthropic API key

When the account an integration calls (the default, or the one it names with
`account=`) was added under the Anthropic option and its listing shows
`billing` `api_key`, pin its key once, while setting up, so the integration
keeps billing that key after the user switches sign-in:

```bash
uv run --no-sync python -c "from use_ai_integration_skill.ai import pin_anthropic_key; print(pin_anthropic_key())"
```

Pass `pin_anthropic_key("<id>")` for a named account. It snapshots that
account's key (and the proxy base URL that goes with it) into
`data/.secrets/anthropic.env`, and refuses any other account.

- Only the key and base URL go in the snapshot, never a subscription token
  (`CLAUDE_CODE_OAUTH_TOKEN`; the writer refuses it).
- The snapshot pins **every** call from code on an Anthropic-option account,
  `complete()` and `run_task()`, to that key; `result.account_id` still names
  the account. A key added through the API key option runs on `pi`, unpinned.
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
  reports none; on a `pi` account it is pi's list price, not a coding or token
  plan's charge).

**Measure before scaling, don't guess.** Run **one real unit** of the work, read
its usage (and its cost, on a key) and wall-clock, and extrapolate to the full
run (`N x per-unit`, plus retries, divided by your pool size for wall-clock).
Tell the user before turning on a volume flow: on a subscription, in calls and
tokens, and that it draws on the same limits as their own chats; on a key, in
dollars. Only when the user asks what a subscription flow would cost on a key,
reprice `result.usage` with litellm's `cost_per_token` for a named model, as an
estimate.

See [references/billing-and-credentialing.md](references/billing-and-credentialing.md)
for the billing buckets, whose use a plan sign-in covers, how credentials are
resolved, and the footguns (a stray provider key in the environment switches a
CLI to that key).
