---
name: use-ai-integration
description: Use when writing or reasoning about code that calls a model -- an AI-driven app or service, an AI integration, or a skill's scripted model step -- including when the user asks for that work to run on their local model (a model on their own computer, Ollama) instead of Claude. Covers the three Claude scenarios (one-shot completion, one-shot agentic task, full agent), the local-model completion, and the cost / credentialing model.
metadata:
  author: imbue
---

# Calling Claude from code

This is the shared reference for the mechanics of calling Claude from code:
which path to use, the call surface, and the cost model. Whatever sent you here
-- building an AI-driven app or service, scripting a skill's `[ai-script]` step, or
adding an AI integration elsewhere -- supplies the framing; this skill is the
how.

Code reaches Claude in one of two ways, depending on whether an
`ANTHROPIC_API_KEY` is configured for the workspace: with a key, call `litellm`
directly; without one, use the `claude -p` helper in this skill's
`python/use_ai_integration_skill/claude_p.py`.

Credentials live in a provider account under `~/.minds/accounts/<id>/`, written
by the provider chooser and pointed at by `CLAUDE_CONFIG_DIR`. NOT in the
process environment -- services inherit a frozen env from supervisord, so an
env-var check goes stale when the user changes auth.

Inside an agent the variable is always set, so a skill script, a worker and the
agent itself all use the account that chat is bound to. Outside one -- a
supervisord service, a cron job -- nothing sets it and `claude_p.py` falls back
to the workspace's default Claude account (the pinned default, else the most
recently used one), for both its resolver and the `claude -p` it launches.
Either way, use its resolver rather than reading a path yourself:

```bash
uv run --no-sync python -c "from use_ai_integration_skill.claude_p import read_workspace_ai_credentials; print('keyed' if read_workspace_ai_credentials().api_key else 'keyless')"
```

**Keyed setups snapshot the key at setup time.** When the check says `keyed`,
copy the API key (and the proxy base URL that goes with it) into
`data/.secrets/anthropic.env` as part of setting up the integration, and have
the service load its credentials from there -- `read_workspace_ai_credentials()`
already resolves that file first, so callers using it get this for free. Run
once while setting up:

```bash
uv run --no-sync python -c "from use_ai_integration_skill.claude_p import write_anthropic_env_snapshot; print(write_anthropic_env_snapshot())"
```

Only the key + base URL go in the snapshot -- NEVER `CLAUDE_CODE_OAUTH_TOKEN`
(a subscription token cannot authenticate direct API calls, and the writer
refuses it). The snapshot pins the integration: if the user later switches the
workspace's sign-in (e.g. to a subscription), built services keep billing
against the key they were set up with. To retire an integration, delete
`data/.secrets/anthropic.env` when the user asks; to re-key one, delete it and run
the snapshot writer again. Never edit the file: the snapshot writer is the one
program-mediated copy the secrets guard allows (its command names neither the
value nor the path), and the guard refuses the direct reads and writes of
`data/.secrets/` it recognises (see the `connect-external-service` skill).

Which path applies rarely changes for a deployment, so **do not branch on it at
call time in simple flows** -- but keyed callers must still resolve the
key/base URL via `read_workspace_ai_credentials()` at each call (not once at
import), so a deliberate re-snapshot takes effect without a service restart.

A caller's model is set **in the code** -- expose it as a top-of-file constant
plus a `--model` override (e.g. `WRITE_MODEL = "claude-haiku-4-5"`), so switching
it is a one-line change. It is independent of the chat's `/model`, which changes
only the conversation.

## Pick the scenario (weakest that does the job)

The call falls into one of three scenarios, by how much agency Claude
needs. Pick the weakest -- it is cheaper, faster, and simpler.

1. **One-shot completion** -- no agency: classify, summarize, extract, rewrite,
   answer-from-context. One prompt, one response, no tools. The common case.
2. **One-shot agentic task** -- a single self-contained job that needs tools or
   file access ("read this file and act", "summarize the diff with the repo
   open"). This is also how you **search the web** -- `claude -p` has a built-in
   `WebSearch` tool.
3. **Full agent** -- a full, possibly long-running agent that runs in its **own
   git worktree** (a `launch-task` worker). Reach for this over scenario 2 when
   Claude edits code that must be tested and validated, or when several agents
   work in the same repo and their changes must not collide. **User- or
   error-triggered only, never an autonomous loop**, with a tightly-scoped task.

## Scenario 1 -- one-shot completion

For a plain completion with **no tools**. If the step needs a tool at all --
web search or otherwise -- reach for an agent (scenario 2), not a server-side
provider tool bolted onto a completion. A server-side tool runs on the provider
that hosts it, welding the step to one vendor, and drags a plain completion onto
a fragile tool code path; an agent's built-in tools have neither problem.

**Keyed (`ANTHROPIC_API_KEY` set): call litellm directly.** It is cheaper than
`claude -p` for non-agentic work, and it gives you structured output, tools,
temperature, etc. with no wrapper of ours in the way. `litellm` is in the root
`pyproject.toml`; read its docs for the call surface. Sketch:

```python
from litellm import completion, completion_cost

from claude_p import read_workspace_ai_credentials  # the file you copied in

# Resolve credentials at call time: the data/.secrets/anthropic.env snapshot
# first (see setup above), then the Claude account's settings, then the process
# env. litellm reads differently-named vars and is picky about a trailing
# slash, so pass both explicitly.
creds = read_workspace_ai_credentials()
api_base = (creds.base_url or "").rstrip("/") or None

resp = completion(
    model="claude-haiku-4-5",
    api_key=creds.api_key,
    api_base=api_base,
    messages=[
        {"role": "system", "content": "You are an email triage classifier."},
        {"role": "user", "content": email_body},
    ],
)
text = resp.choices[0].message.content
cost = completion_cost(completion_response=resp)  # USD for this call
```

**Keyless (no key): copy this skill's `python/use_ai_integration_skill/claude_p.py` and call `claude_p_completion`.**
It disables tools and runs from an isolated working directory so the repo's
`CLAUDE.md` / `.claude` hooks can't hijack the answer; `system` is required.

```python
from claude_p import claude_p_completion  # the file you copied in

result = claude_p_completion(
    "Classify this email's intent:\n\n" + email_body,
    system="You are an email triage classifier.",   # required
    model="claude-haiku-4-5",
)
print(result.text, result.cost_usd, result.usage)
```

Both `completion` and `claude_p_completion` are synchronous (no asyncio). Once
you have confirmed the prompt + model combination works and produces good
results on a few items, run a batch concurrently with a thread pool
(`concurrent.futures.ThreadPoolExecutor`) rather than one at a time -- the
throughput difference is large. Use enough workers to actually saturate the work
(the calls are I/O-bound, so this can be well into the dozens); back off only if
you hit provider rate limits. When you need structured output, prefer the
provider's own JSON / structured-output mode over parsing free text and retrying
-- it is what keeps the response well-formed.

## On the user's local model -- one-shot completion only

Use this path when, and only when, **the user asks for it** ("do the refreshes
with the local model", "use my Ollama for the summaries", "keep this off the
API"). It runs a scenario 1 completion on a model the user hosts on their own
computer, at no cost and with nothing leaving their machines. It is not a
cheaper default: a local model is smaller and slower than Claude, so moving work
onto it without being asked trades away quality the user did not offer.

The workspace reaches that model through its **Local model** connection (the
provider of that name in a chat's provider menu). Copy this skill's
`python/use_ai_integration_skill/local_model.py` and call
`local_model_completion`; it reads the connection's address, model and key from
the account, so the code names none of them.

```python
from local_model import LocalModelUnreachableError, local_model_completion  # the file you copied in

try:
    result = local_model_completion(
        "Summarize this article in two sentences:\n\n" + article_text,
        system="You write short, plain news summaries. Answer with the summary only.",  # required
    )
except LocalModelUnreachableError as e:
    # The user's computer is asleep or offline. Not a bug: skip, and say so.
    skip_this_run(reason=str(e))
print(result.text, result.duration_seconds)
```

Check that the connection exists before building on it. With none, this prints
the instruction to pass on to the user rather than an endpoint:

```bash
uv run --no-sync python -c "from use_ai_integration_skill.local_model import read_local_model_endpoint; print(read_local_model_endpoint())"
```

What is different from Claude, and what to build for:

- **It is not always there.** The model answers only while the user's computer
  is awake, its model server is running and, for a workspace that is not on that
  computer, Imbue Studio is open. A scheduled refresh will run into nights when
  it is not. Catch `LocalModelUnreachableError`, keep the data the app already
  has, and record that the run was skipped and why (for a routine, in
  `$ROUTINES_RUN_DIR/result.json`; in an app, beside the "last refreshed" time
  it shows). Never fall back to Claude for a run the user asked to keep local.
- **One call at a time.** A local server answers requests in turn, so a thread
  pool only queues them. Loop over the items, and expect seconds to minutes
  each; the first call after a pause also waits for the model to load. Size a
  refresh to that: a routine's default timeout is an hour.
- **One item per call, short and explicit.** The window (`context_window` on the
  endpoint, often 40,960 tokens) holds the prompt and the answer together. Send
  one article, not twenty. Say exactly what to answer with; a small model
  wanders where Claude would not.
- **Ask for JSON when you need structure:**
  `response_format={"type": "json_object"}`, with the shape spelled out in
  `system`. Validate what comes back and skip an item that does not parse rather
  than failing the run.
- **Completions only.** No tools, no file access, no web search. A step that
  needs any of those is a scenario 2 or 3 job and stays on Claude; say so to the
  user if they asked for all of it to be local.
- **Reasoning is off unless you ask.** A reasoning model would otherwise think
  before every answer, which for a summary or a label is most of the wait.
  `is_reasoning_wanted=True` turns it on for a step that needs it; raise
  `max_tokens` when you do, since the thinking counts against it.
- **Nothing to cost out.** Skip the cost measurement below; measure wall-clock
  on one real item instead, and tell the user how long a full run takes.

A model other than the one the connection was signed in on is named by its
Ollama name (`model="qwen3:8b"`), and has to have been used in a chat first.

## Scenario 2 -- one-shot agentic task

Always `claude -p` (it has tools and file access; a plain API call does not), so
this path is the same whether or not a key is set. Copy this skill's
`python/use_ai_integration_skill/claude_p.py` and
call `claude_p_task`: tools stay enabled, it runs in the repo working directory,
and it defaults `permission_mode="bypassPermissions"` (load-bearing -- a headless
run has no human to approve tool use).

```python
from claude_p import claude_p_task

result = claude_p_task(
    "Read data/.apps/email-triage/latest.json and draft a reply using templates/.",
    append_system="Only touch files under data/.apps/email-triage/.",
)
```

`append_system` layers instructions on the default agent; pass `system` to
replace it outright. The default agent prompt is many tokens, but it is useful
instruction for agentic work, so overwrite it only when you have a good reason.
Cost is dominated by per-call overhead, so **batch** items into fewer, larger
calls rather than one call per item.

## Scenario 3 -- full agent

Reach for this over scenario 2 when the work needs its **own git worktree**:
Claude is editing code that has to be tested and validated, or other agents are
working in the same repo and the changes must not collide. A `launch-task` worker
gives the run an isolated branch and worktree; scenario 2 instead runs in the
caller's own working directory.

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

## Cost and the keyed onramp

A keyless caller can tell the user what each call costs and what a key would save,
so they can decide when volume justifies setting `ANTHROPIC_API_KEY`:

- `claude_p_completion` / `claude_p_task` return the **actual** `cost_usd` that
  `claude -p` reported, plus the token `usage`.
- Reprice that usage at the keyed model's rate with litellm to estimate the
  savings -- no price table to maintain, litellm carries the prices:

  ```python
  from litellm import cost_per_token

  prompt_cost, completion_cost = cost_per_token(
      model="claude-haiku-4-5",
      prompt_tokens=result.usage.input_tokens,
      completion_tokens=result.usage.output_tokens,
  )
  keyed_estimate = prompt_cost + completion_cost
  savings = result.cost_usd - keyed_estimate   # surface this to suggest a key
  ```

- **Measure before scaling, don't guess.** Run **one real unit** of the work,
  read its **actual** cost and wall-clock off the response (`completion_cost(...)`
  or `result.cost_usd`, not a token estimate), and extrapolate to the full run
  (`N x per-unit`, plus any retries and per-tool/search fees, divided by your pool
  size for wall-clock). Tell the user that projected cost/time before you turn on
  a volume flow.

See [references/billing-and-credentialing.md](references/billing-and-credentialing.md)
for the billing buckets, why `claude -p` costs more than the direct API, the
credentialing model, and the footgun (a stray `ANTHROPIC_API_KEY` switches
`claude -p` to full-API billing).
