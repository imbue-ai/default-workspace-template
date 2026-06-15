# Calling Claude from code

How a script or service makes a Claude call and accounts for its cost. Two
consumers share this: a service built with the `use-ai-integration` skill, and a
skill's own `[ai-script]` step (see `spec-summary.md`). For the deep billing
model and credentialing, see the use-ai-integration skill's
[billing-and-credentialing.md](../../skills/use-ai-integration/references/billing-and-credentialing.md).

## Pick the path (weakest that does the job)

The path turns on how much agency the call needs and whether `ANTHROPIC_API_KEY`
is set. Pick the weakest -- it is cheaper, faster, and simpler.

- **One-shot completion** -- no agency: classify, summarize, extract, rewrite,
  answer-from-context. One prompt, one response, no tools. The common case.
  - **Keyed** (`ANTHROPIC_API_KEY` set): call `litellm` directly. Cheaper than
    `claude -p` for non-agentic work, and it gives structured output / tools /
    temperature with no wrapper in the way. `litellm` is in the root
    `pyproject.toml`.
  - **Keyless** (no key): copy `claude_p.py` (from the use-ai-integration
    skill's `scripts/`) and call `claude_p_completion`.
- **One-shot agentic task** -- needs tools or file access ("read this file and
  act"). Always `claude -p` (a plain API call has no tools), so this path is the
  same whether or not a key is set: copy `claude_p.py` and call `claude_p_task`.
- **Full agent** -- a long-running agent in its own git worktree. This is a
  `launch-task` worker, not a single call; see the `use-ai-integration` skill.

Which path applies is **fixed for a deployment** -- it does not change at
runtime. Check once, up front, and implement only the path that applies rather
than branching on the key at call time:

```bash
[ -n "$ANTHROPIC_API_KEY" ] && echo keyed || echo keyless
```

## One-shot completion

Keyed -- call `litellm` directly:

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

Keyless -- the copied `claude_p.py` helper. It disables tools and runs from an
isolated working directory so the repo's `CLAUDE.md` / `.claude` hooks can't
hijack the answer; `system` is required:

```python
from claude_p import claude_p_completion  # the file you copied in

result = await claude_p_completion(
    "Classify this email's intent:\n\n" + email_body,
    system="You are an email triage classifier.",   # required
    model="claude-haiku-4-5",
)
print(result.text, result.cost_usd, result.usage)
```

Both `acompletion` and `claude_p_completion` are async. Once the prompt + model
combination works on a few items, run a batch concurrently (an `anyio` task
group) rather than awaiting them one at a time -- the throughput difference is
large.

## One-shot agentic task

Copy `claude_p.py` and call `claude_p_task`: tools stay enabled, it runs in the
repo working directory, and it defaults `permission_mode="bypassPermissions"`
(load-bearing -- a headless run has no human to approve tool use):

```python
from claude_p import claude_p_task

result = await claude_p_task(
    "Read runtime/email-triage/latest.json and draft a reply using templates/.",
    append_system="Only touch files under runtime/email-triage/.",
)
```

`append_system` layers instructions on the default agent; pass `system` to
replace it outright. The default agent prompt is many tokens but useful for
agentic work, so overwrite it only with good reason. Cost is dominated by
per-call overhead, so **batch** items into fewer, larger calls.

## Surface the cost

A keyless caller can tell the user what each call costs and what a key would
save, so they can decide when volume justifies setting `ANTHROPIC_API_KEY`:

- `claude_p_completion` / `claude_p_task` return the **actual** `cost_usd`
  `claude -p` reported, plus the token `usage`; `litellm` exposes
  `completion_cost(...)`.
- Reprice keyless usage at the keyed model's rate to estimate the savings --
  litellm carries the prices, so there is no table to maintain:

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

- **Measure on a small sample before scaling.** Run on a handful of items, check
  the cost, and tell the user the projected cost before turning on a volume flow.
