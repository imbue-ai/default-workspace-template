---
name: use-ai-integration
description: Use when building a service that calls Claude -- an AI-driven service or AI integration. Covers the three scenarios (one-shot completion, one-shot agentic task, full agent), choosing between a keyed litellm call and the keyless claude -p helper, and the cost / credentialing model.
---

# Calling Claude from a service

A service's call to Claude falls into one of three scenarios, by how much agency
Claude needs. Pick the weakest -- it is cheaper, faster, and simpler.

1. **One-shot completion** -- no agency: classify, summarize, extract, rewrite,
   answer-from-context. One prompt, one response, no tools. The common case.
2. **One-shot agentic task** -- a single self-contained job that needs tools or
   file access ("read this file and act", "summarize the diff with the repo
   open").
3. **Full agent** -- a full, possibly long-running agent that runs in its **own
   git worktree** (a `launch-task` worker), covered below.

Scenarios 1 and 2 -- the path choice (a keyed `litellm` call vs the keyless
`claude_p.py` helper, and `claude_p_task` for the agentic case), the call
surface, and surfacing cost -- are shared with skill `[ai-script]` steps and
documented in one place:
**[../../shared/references/calling-claude.md](../../shared/references/calling-claude.md)**.
Read it for those two scenarios before writing the service.

For deep billing and credentialing -- billing buckets, why `claude -p` costs
more than the direct API, and the footgun (a stray `ANTHROPIC_API_KEY` switches
`claude -p` to full-API billing) -- see
[references/billing-and-credentialing.md](references/billing-and-credentialing.md).

## Scenario 3 -- full agent

Reach for this over scenario 2 when the work needs its **own git worktree**:
Claude is editing code that has to be tested and validated, or other agents are
working in the same repo and the changes must not collide. A `launch-task` worker
gives the run an isolated branch and worktree; scenario 2 instead runs in the
service's own working directory.

Launch the worker synchronously and collect its structured result -- do not wrap
it; call the script directly:

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

Cost for a full agent is dominated by the run itself; the per-call cost
surfacing in [calling-claude.md](../../shared/references/calling-claude.md)
applies to scenarios 1 and 2.
