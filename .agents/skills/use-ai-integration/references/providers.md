# Account types in detail

How the library calls each kind of account, what a call there can do, which
model runs when `model` is `None`, and what the CLI leaves in the account
folder. SKILL.md has the summary table.

## Anthropic

- **Claude plan, long-lived token, Console sign-in**: `claude -p` with
  `CLAUDE_CONFIG_DIR` set to the account folder. `complete()` disables every
  tool, replaces the system prompt with `system` and runs from an empty
  directory; `run_task()` bypasses permission prompts in `cwd`. Default model:
  `DEFAULT_ANTHROPIC_MODEL`.
- **API key**: `complete()` calls litellm with the key (and its proxy base
  URL); `run_task()` hands the key to `claude -p`. A key pinned with
  `pin_anthropic_key()` is used for every Anthropic account.

## OpenAI

- **ChatGPT plan**: `codex exec` with `CODEX_HOME` set to the account folder.
  `complete()` runs from an empty directory in a read-only sandbox, with the
  account's config and rules ignored. It is still an agent that can run
  commands that read files, and `system` only leads the prompt. Default model:
  the account's own (`result.model` is `None`).
- **API key**: `complete()` calls litellm (default `DEFAULT_OPENAI_API_MODEL`);
  `run_task()` runs `codex exec` on the key, which reports no cost.

## Google sign-in

`agy` (the Antigravity CLI) in print mode, with `HOME` set to the account
folder and its self-updater off. The prompt goes on standard input; agy has no
system-prompt option, so `system` (or `instructions`) leads the prompt.

- `complete()` runs from an empty directory with session settings that pin
  agy's Default mode and strict tool permissions: a command or file edit asks
  for approval, and with no one to approve it agy refuses it and carries on.
  It is still an agent turn, and tools that need no approval (reading the
  empty directory, and whatever agy's own permission settings allow, such as
  reading web pages) stay available.
- `run_task()` approves every tool (`--dangerously-skip-permissions`) in
  `cwd`. agy will not take a folder with a dot-prefixed path segment as its
  project, so for such a `cwd` the library runs it from a link under `/tmp`.
- Default model: the account's own (`result.model` is `None`). Pass a model
  slug that `agy models` lists to choose one.
- Billing: the Google AI plan's quota, reported as `subscription`; agy reports
  tokens, not dollars. A sign-in through a Google Cloud project can bill that
  project per token instead, which the library cannot tell apart.
- agy keeps each run's conversation and logs under `.gemini/antigravity-cli/`
  in the account folder, as chats on that account do.
- Google's [Antigravity FAQ](https://antigravity.google/docs/faq#can-i-use-antigravity-cli-headless-mode-in-third-party-coding-agents-or-automate-account-switching)
  says headless `agy -p` is meant for direct scripting and CI, and that
  wrapping the CLI inside third-party coding agents or apps is not
  supported on consumer plans. An app other people use calls Gemini on a Gemini API key:
  add one through the API key option as Google Gemini.

## Opencode Go, OpenRouter, and keys added through the API key option

`pi` with `PI_CODING_AGENT_DIR` set to the account folder, `--provider` set to
the one provider in the folder's `auth.json`, an explicit `--model`, and no
provider key in its environment, so the call runs on that key alone. The
prompt goes on standard input; pi drops its leading and trailing whitespace.

- `complete()`: no tools, `system` as the whole system prompt, thinking off,
  no session saved, and no `AGENTS.md` or `CLAUDE.md`, extensions, skills or
  prompt templates, from an empty directory.
- `run_task()`: pi's default tools (read, bash, edit, write) and its coding
  prompt with `instructions` appended, in `cwd`, where it reads `AGENTS.md`.
  pi has no approval step.
- Default model: `DEFAULT_PI_MODELS[provider]`, pi's own default for that
  provider, except the small `DEFAULT_ANTHROPIC_MODEL` and
  `DEFAULT_OPENAI_API_MODEL` for Anthropic and OpenAI keys. A provider with no
  entry raises `ModelRequiredError`; pass `model=`.
- Billing: Opencode Go is a flat subscription (`subscription`, no cost). Every
  other key is `api_key`, and `cost_usd` is pi's price for the tokens. For a
  coding or token plan's key (ZAI Coding Plan, Kimi For Coding, Qwen Token
  Plan, Xiaomi MiMo Token Plan) that is pi's list price, or 0, not what the
  plan charges.
- pi retries a failed model request up to three times before the call raises,
  and keeps a model catalogue cache, `models-store.json`, in the account
  folder.
