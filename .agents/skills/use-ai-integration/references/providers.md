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
  `pin_anthropic_key()` is used for every account added under the Anthropic
  option.
- An Anthropic key added through the API key option is a `pi` account (below):
  it is never pinned, and a Claude subscription token (`sk-ant-oat...`) pasted
  there is refused, because pi would send it as a plan sign-in. Add such a token
  under the Anthropic option's "Get a long-lived token".

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
  agy's Default mode and ask for approval of every command and file edit, which
  no one is there to give. It is still an agent turn: agy has no tools-off
  option, and tools that need no approval (such as reading web pages, where
  agy's own permission settings allow it) stay available.
- `run_task()` approves every tool (`--dangerously-skip-permissions`) in
  `cwd`. agy will not take a folder with a dot-prefixed path segment as its
  project, so for such a `cwd` the library runs it from a link under `/tmp`.
- Default model: the account's own (`result.model` is `None`). To choose one,
  pass a slug from
  `HOME=/home/user/.minds/accounts/<id> AGY_CLI_DISABLE_AUTO_UPDATE=true agy models`.
- Billing: the Google AI plan's quota, reported as `subscription`; agy reports
  tokens, not dollars. A Workspace sign-in on a Google Cloud project's Pay As
  You Go is billed per token at Cloud API prices instead, which the library
  cannot tell apart.
- agy keeps each run's conversation and logs under `.gemini/antigravity-cli/`
  in the account folder, as chats on that account do.
- Google's [Antigravity FAQ](https://antigravity.google/docs/faq) says headless
  `agy -p` is meant for direct scripting and CI, and calls wrapping the CLI in
  any third-party app or coding agent unsupported on consumer plans. Its
  [Terms](https://antigravity.google/terms), clause 6, say using third-party
  software to access the service is a breach that may be grounds for
  suspending or terminating the Antigravity and Gemini CLI accounts. Keep a
  Google sign-in for the owner's own direct scripting. Any app or automation
  calls Gemini on a Gemini API key: add one through the API key option as
  Google Gemini.

## Opencode Go, OpenRouter, and keys added through the API key option

`pi` with `PI_CODING_AGENT_DIR` set to the account folder, `--provider` set to
the one provider in the folder's `auth.json`, an explicit `--model`, and no
provider key, endpoint or billing-account variable in its environment, so the
call runs on that key alone. The prompt goes on standard input; pi drops its
leading and trailing whitespace.

- `complete()`: no tools, `system` in place of pi's own prompt (pi appends only
  the working directory's path), thinking off, no session saved, and no
  `AGENTS.md` or `CLAUDE.md`, extensions, skills or prompt templates, from an
  empty directory. An empty `system` raises `EmptySystemPromptError`, since pi
  would run its coding-agent prompt instead.
- `run_task()`: pi's default tools (read, bash, edit, write) and its coding
  prompt with `instructions` appended, in `cwd`, where it reads `AGENTS.md`.
  pi has no approval step and no web tool.
- Default model: `DEFAULT_PI_MODELS[provider]`, pi's own default for that
  provider, except the small `DEFAULT_ANTHROPIC_MODEL` and
  `DEFAULT_OPENAI_API_MODEL` for Anthropic and OpenAI keys. A provider with no
  entry raises `ModelRequiredError`; pass `model=`.
- Model ids are pi's ids for the provider, e.g. `anthropic/claude-sonnet-4.5`
  on OpenRouter. List them with
  `PI_CODING_AGENT_DIR=/home/user/.minds/accounts/<id> pi --list-models <search>`
  and pass an exact id: pi matches a partial id to the closest model, and runs
  an unknown id as a custom model priced like the provider's default, while
  `result.model` still echoes the id passed.
- Billing: Opencode Go is a subscription (`subscription`, no cost). Once Extra
  Usage is on (the default on Go Teams) it falls back to the pay-as-you-go
  balance after its limits, and Go is meant for coding-agent traffic, so
  high-volume completions belong on a per-token key. Every other key is
  `api_key`, and `cost_usd` is pi's price for the tokens. A coding or token
  plan's key (ZAI Coding Plan, Kimi For Coding, Qwen Token Plan, Xiaomi MiMo
  Token Plan) draws on the plan's quota, shared with pi chats on that key, and
  its `cost_usd` is pi's list price, or 0, not what the plan charges.
- pi retries a failed model request up to three times before the call raises,
  and keeps a model catalogue cache, `models-store.json`, in the account
  folder. Concurrent calls on one account share that file.
