---
name: use-ai-integration
description: Use when building a service that calls Claude -- AI-driven services and AI integrations. Covers the three integration patterns (one-shot completion, one-shot agentic task, full agent), how to pick one, and the credentialing / billing / cost model. Backed by the libs/ai_integration package.
---

# Use an AI integration in a service

A service can call Claude in three ways, at escalating levels of agency. Pick the
weakest one that does the job -- it is cheaper, faster, and simpler. All three live
in the `ai_integration` library, so you don't hand-roll credentialing, the
`claude -p` environment fix, billing-path logging, or spend control.

```python
from ai_integration.core import run_completion, run_task, run_agent
```

The functions are `async` (services here are async FastAPI).

## Pick the pattern

1. **No agency** -- classify, summarize, extract, rewrite, answer-from-context?
   Use **`run_completion`**. The common case, and the cheapest.
2. **Agency, one self-contained run** -- "read this email and file a ticket",
   "summarize this diff with the repo open"? Use **`run_task`** (one headless
   `claude -p` run).
3. **A full, possibly long-running agent** -- the service edits itself on user
   feedback, or spins up an agent to fix an error? Use **`run_agent`**. Must be
   **user- or error-triggered, never an autonomous loop**, with a tightly-scoped
   task.

See [references/patterns.md](references/patterns.md) for a worked sketch of each.

## Pattern 3 -- `run_completion` (no agency)

```python
result = await run_completion(
    "Classify this email's intent:\n\n" + email_body,
    service_name="email-triage",       # resolves the spend ceiling, if any
    model="claude-haiku-4-5",          # cheap default; override as needed
    system="You are an email triage classifier.",
)
print(result.text, result.billing_path, result.cost_usd)
```

- **Routing is implicit by key presence**: direct Anthropic API when
  `ANTHROPIC_API_KEY` is set (cheaper for non-agentic work), else headless
  `claude -p`. You don't choose.
- **`system` is required.** Make it a real instruction, not a placeholder.
- **`anthropic_options` and structured output are direct-API-only.** Any Messages
  API param (`tools`, `tool_choice`, `temperature`, ...) passes through
  `anthropic_options=...` but is honored only on the keyed path (the keyless
  fallback ignores them and warns). With `tools` + `tool_choice` the model answers
  with a tool call -- read it from **`result.tool_calls`** (`ToolCall(name, input,
  id)`); `result.text` is empty then.

A user with no API key builds and tests on the `claude -p` fallback immediately;
setting `ANTHROPIC_API_KEY` later upgrades every call to the cheaper direct API
with no code change. The keyless path logs the savings a key would unlock, so
don't push the user to set one up front -- surface the figure once volume
justifies it.

## Pattern 2 -- `run_task` (one-shot agentic)

```python
result = await run_task(
    "Read runtime/email-triage/latest.json and draft a reply; "
    "use the repo's templates in templates/.",
    service_name="email-triage",
)
```

- Always `claude -p` (it has tools and file access; direct API does not). Tools
  stay enabled -- the point is to ride the default agent.
- `append_system="..."` layers task instructions on the default agent prompt;
  `system="..."` replaces it (rare).
- Defaults `permission_mode="bypassPermissions"` because a headless agent has no
  human to approve tool use -- without it, Read/Write/Bash are auto-denied. Tighten
  it (e.g. `"acceptEdits"`) or set `None` and pass your own `--allowedTools` via
  `claude_cli_args` for a narrower grant.

## Pattern 1 -- `run_agent` (full agent)

```python
from ai_integration.data_types import AgentOutcome

result = await run_agent(
    name="email-triage-selfedit-42",
    template="worker",
    runtime_dir=Path("runtime/email-triage/selfedit-42"),
    task_file=Path("runtime/email-triage/selfedit-42/task.md"),
    service_name="email-triage",
)
if result.outcome is AgentOutcome.DONE:
    ...  # the worker's branch is result.branch
```

Wraps the `launch-task` synchronous path (`create_worker.py launch-sync`: launch
-> await finish report -> structured result -> destroy). Write the task file first
with `lead_agent` / `finish_report_path` frontmatter (see the `launch-task`
skill). **User- or error-triggered only**, with a **tightly-scoped** task -- a
broad unattended launch is how cost and time run away. What to do with the
returned branch (merge, review) is out of scope here.

## Cost control

`claude -p` and the direct API draw separate pools from interactive usage, so
service calls never block the user's chat (see
[references/billing-and-credentialing.md](references/billing-and-credentialing.md)).
The live concern is **cost**:

- **Measure on a small sample before scaling.** Run the pattern on a handful of
  items, check `result.cost_usd`, and tell the user the projected cost. For
  `run_task`, cost is dominated by per-call overhead, so **batch rather than
  parallelize** (fewer, larger calls).
- **Confirm the billing path and rough cost with the user before turning on a
  volume flow.**
- **Offer a spend ceiling (optional, `services.toml`).** No tracker to construct
  or pass -- it's resolved automatically and keyed by `service_name`, aggregating
  across every call for that service (persisted under `runtime/<service>/`):

  ```toml
  [services.email-triage.ai_spend]
  ceiling_usd = 5.0          # rolling-window budget
  window_seconds = 86400     # optional; default 24h
  ```

  Each call then checks the ceiling first and records its cost after; once the
  window's spend hits the ceiling, the next call raises
  `SpendCeilingExceededError` instead of spending silently (catch it to notify the
  user via `send-user-message`). No `ai_spend` table -> unbounded. It's opt-in:
  tell the user it's available and let them decide. The table works with no
  `command` too (spend-tracking-only service).

## What the library handles for you

Credentialing (raises `CredentialsUnavailableError` loudly if no credential
resolves), the mngr `claude -p` session-hook fix, billing-path logging plus the
keyless savings nudge, context isolation on the keyless completion path, and
surfacing forced tool calls in `result.tool_calls`. Details, and the footgun (a
stray `ANTHROPIC_API_KEY` silently switches `claude -p` to full-API billing), are
in [references/billing-and-credentialing.md](references/billing-and-credentialing.md).
