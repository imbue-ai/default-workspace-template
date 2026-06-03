# The three patterns, worked

## Pattern 3 -- `run_completion` (no agency)

When: classify / summarize / extract / rewrite / answer-from-context. No tools,
no file access, one prompt -> one response.

```python
result = await run_completion(
    prompt,
    service_name="my-service",
    model="claude-haiku-4-5",
    system="optional system prompt (cached automatically)",
    anthropic_options={"temperature": 0},   # any Messages API param
    spend_tracker=tracker,
)
text = result.text
```

- Routing is implicit: direct API if `ANTHROPIC_API_KEY`, else `claude -p`.
- Default model is the cheapest tier; override per call.
- Structured output: pass the relevant Messages API options through
  `anthropic_options`.

## Pattern 2 -- `run_task` (one-shot agentic)

When: a single self-contained job that needs tools or file access -- "read this
file and act", "open the repo and summarize the diff".

```python
result = await run_task(
    "Read runtime/x/input.json and write runtime/x/output.json with ...",
    service_name="my-service",
    spend_tracker=tracker,
)
```

- Always `claude -p` (agentic). No direct-API option.
- Cost is dominated by per-call overhead (each invocation reloads the agent
  context). **Batch** many items into fewer, larger calls instead of spawning one
  call per item.

## Pattern 1 -- `run_agent` (full agent)

When: a full agent is warranted -- the service edits itself in response to
feedback, or launches an agent to fix an error. Equivalent to handing the work
back to a real agent session.

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

Rules:

- **User- or error-triggered only.** Never an autonomous loop.
- **Tightly-scoped task.** Write the task file with a narrow, well-defined goal
  and a `finish_report_path`; a broad task in an unattended launch is how cost and
  time run away. (Agents are launched by *service* events, not by other agents.)
- The wrapper launches, waits for the finish report, returns a structured
  `AgentResult` (`outcome`, `body`, `branch`), and destroys the agent. The branch
  survives. Applying the result (merge / review) is a separate concern.
- Writing the task file and its frontmatter follows the `launch-task` skill.
