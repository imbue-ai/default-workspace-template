# The three patterns, worked

## Pattern 3 -- `run_completion` (no agency)

When: classify / summarize / extract / rewrite / answer-from-context. No tools, no
file access, one prompt -> one response.

```python
result = await run_completion(
    prompt,
    system="You are an email triage classifier.",   # REQUIRED
    service_name="my-service",
    model="claude-haiku-4-5",
    anthropic_options={"temperature": 0},   # any Messages API param; direct-API path only
)
text = result.text
```

- Routing is implicit: direct API if `ANTHROPIC_API_KEY`, else `claude -p`.
- Default model is the cheapest tier; override per call.
- Structured output / `anthropic_options` are honored only on the keyed path;
  forced tool calls arrive in `result.tool_calls`.
- `system` is required and must be a real instruction.
- Spend ceiling is optional and config-driven (see the skill's "Cost control").

## Pattern 2 -- `run_task` (one-shot agentic)

When: a single self-contained job needing tools or file access -- "read this file
and act", "open the repo and summarize the diff".

```python
result = await run_task(
    "Read runtime/x/input.json and write runtime/x/output.json with ...",
    service_name="my-service",
    append_system="Only touch files under runtime/x/.",  # optional, layered on default
)
```

- Always `claude -p`. No direct-API option. Tools stay enabled.
- `system` / `append_system` optional (the default agent is the point):
  `append_system` adds task instructions on top, `system` replaces outright.
- Cost is dominated by per-call overhead, so **batch** items into fewer, larger
  calls rather than one call per item.

## Pattern 1 -- `run_agent` (full agent)

When: a full agent is warranted -- the service edits itself on feedback, or
launches an agent to fix an error.

```python
result = await run_agent(
    name="my-service-fix-123",
    template="worker",
    runtime_dir=Path("runtime/my-service/fix-123"),
    task_file=Path("runtime/my-service/fix-123/task.md"),
    service_name="my-service",
    timeout="30m",
)
```

- **User- or error-triggered only.** Never an autonomous loop.
- **Tightly-scoped task.** Write the task file with a narrow goal and a
  `finish_report_path`; a broad unattended launch is how cost and time run away.
- The wrapper launches, waits for the finish report, returns an `AgentResult`
  (`outcome`, `body`, `branch`), and destroys the agent. The branch survives;
  applying the result (merge / review) is a separate concern.
- Task-file frontmatter follows the `launch-task` skill.
