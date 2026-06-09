# ai_integration

Helpers for calling Claude from a service, at three escalating levels of agency.
Credentialing, the `claude -p` environment fix (`MAIN_CLAUDE_SESSION_ID`),
billing-path logging, and per-service spend control are handled for you.

- `run_completion(prompt, *, system, ...)` -- no agency. Direct Anthropic API when
  `ANTHROPIC_API_KEY` is set, else `claude -p`; routing is implicit by key
  presence. `system` is **required**. `anthropic_options` (tools, `tool_choice`,
  ...) and structured output via `result.tool_calls` are honored only on the keyed
  path; the keyless path ignores them and warns.
- `run_task(...)` -- one-shot agentic task (tools / file access) via `claude -p`;
  tools stay enabled, defaulting `permission_mode="bypassPermissions"` so the
  headless agent can use them. Optional `system` / `append_system` shape the agent.
- `run_agent(...)` -- a full agent via the `launch-task` synchronous
  launch -> await -> collect -> destroy path.

Spend control is opt-in via `services.toml`, not a tracker passed in code: add
`[services.<service_name>.ai_spend]` with `ceiling_usd` (and optional
`window_seconds`) and `run_completion` / `run_task` enforce it automatically, keyed
by `service_name` and aggregated across every call. No table -> unbounded.

See the `use-ai-integration` skill for when to pick each pattern and the billing
model.
