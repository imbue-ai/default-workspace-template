---
name: use-ai-integration
description: Use when building a service that calls Claude -- an AI-driven service or AI integration. Covers the three scenarios (one-shot completion, one-shot agentic task, full agent), choosing between a keyed litellm call and the keyless claude -p helper, and the cost / credentialing model.
---

# Calling Claude from a service

A service reaches Claude in one of two ways, and **you choose when you write the
service** -- there is no runtime router and no library to import. The choice is
driven entirely by whether `ANTHROPIC_API_KEY` is set in the service's
environment:

```python
import os

if os.environ.get("ANTHROPIC_API_KEY"):
    ...  # keyed path: call litellm directly (below)
else:
    ...  # keyless path: copy and use scripts/claude_p.py (below)
```

## Pick the scenario (weakest that does the job)

A service's need falls into one of three scenarios, by how much agency Claude
needs. Pick the weakest -- it is cheaper, faster, and simpler.

1. **One-shot completion** -- no agency: classify, summarize, extract, rewrite,
   answer-from-context. One prompt, one response, no tools. The common case.
2. **One-shot agentic task** -- a single self-contained job that needs tools or
   file access ("read this file and act", "summarize the diff with the repo
   open").
3. **Full agent** -- a full, possibly long-running agent (the service edits
   itself on feedback, or launches an agent to fix an error). **User- or
   error-triggered only, never an autonomous loop**, with a tightly-scoped task.

## Scenario 1 -- one-shot completion

**Keyed (`ANTHROPIC_API_KEY` set): call litellm directly.** It is cheaper than
`claude -p` for non-agentic work, and it gives you structured output, tools,
temperature, etc. with no wrapper of ours in the way. `litellm` is in the root
`pyproject.toml`; read its docs for the call surface. Sketch:

```python
from litellm import acompletion, completion_cost

resp = await acompletion(
    model="claude-haiku-4-5",
    messages=[
        {"role": "system", "content": "You are an email triage classifier."},
        {"role": "user", "content": email_body},
    ],
)
text = resp.choices[0].message.content
cost = completion_cost(completion_response=resp)  # USD for this call
```

**Keyless (no key): copy `scripts/claude_p.py` and call `claude_p_completion`.**
It disables tools and runs from an isolated working directory so the repo's
`CLAUDE.md` / `.claude` hooks can't hijack the answer; `system` is required.

```python
from claude_p import claude_p_completion  # the file you copied in

result = await claude_p_completion(
    "Classify this email's intent:\n\n" + email_body,
    system="You are an email triage classifier.",   # required
    model="claude-haiku-4-5",
)
print(result.text, result.cost_usd, result.usage)
```

## Scenario 2 -- one-shot agentic task

Always `claude -p` (it has tools and file access; a plain API call does not), so
this path is the same whether or not a key is set. Copy `scripts/claude_p.py` and
call `claude_p_task`: tools stay enabled, it runs in the repo working directory,
and it defaults `permission_mode="bypassPermissions"` (load-bearing -- a headless
run has no human to approve tool use).

```python
from claude_p import claude_p_task

result = await claude_p_task(
    "Read runtime/email-triage/latest.json and draft a reply using templates/.",
    append_system="Only touch files under runtime/email-triage/.",
)
```

`append_system` layers instructions on the default agent; pass `system` to
replace it (rare). Cost is dominated by per-call overhead, so **batch** items into
fewer, larger calls rather than one call per item.

## Scenario 3 -- full agent

Launch a `launch-task` worker synchronously and collect its structured result --
do not wrap it; call the script directly:

```bash
uv run .agents/skills/launch-task/scripts/create_worker.py launch-sync \
  --name email-triage-fix-123 --template worker \
  --runtime-dir runtime/email-triage/fix-123 \
  --task-file  runtime/email-triage/fix-123/task.md \
  --timeout 30m --result-json runtime/email-triage/fix-123/result.json
```

It launches, waits for the worker's finish report in the foreground, writes a JSON
result (`timed_out`, `type`, `name`, `body`, `branch`, `raw_report`) to
`--result-json`, and destroys the worker (the `mngr/<name>` branch survives).
Write the task file first with `lead_agent` / `finish_report_path` frontmatter
(see the `launch-task` skill). **User- or error-triggered, tightly scoped** -- a
broad unattended launch is how cost and time run away. What to do with the
returned branch (merge, review) is your concern.

## Cost and the keyed onramp

A keyless service can tell the user what each call costs and what a key would save,
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

- **Measure on a small sample before scaling.** Run the scenario on a handful of
  items, check the cost, and tell the user the projected cost before turning on a
  volume flow. There is no spend ceiling -- if you want one, build it yourself.

See [references/billing-and-credentialing.md](references/billing-and-credentialing.md)
for the billing buckets, why `claude -p` costs more than the direct API, the
credentialing model, and the footgun (a stray `ANTHROPIC_API_KEY` switches
`claude -p` to full-API billing).
