# Hardening a scripted Claude call

**Worker reference.** Read this when you are the background harden / crystallize
worker turning a scripted `[ai-script]` step (or an AI-driven service) into
production code. A lead building an interactive sample does not need any of it --
the lead does the processing in-context and hands the model-calling work to you.
The `SKILL.md` body carries the decisions (which scenario, which model, use an
agent for search); this reference carries the plumbing.

## Measure cost and time: one real unit, then extrapolate

Before you scale a metered or fanned-out step -- and as a **required field in the
Gate 1 outline** (`.agents/shared/worker/references/skill-outline-fields.md`) --
produce a *measured* cost/time estimate, not a guess:

1. Run **one real unit** of the metered work (one item's call, with whatever
   tools / web search it uses).
2. Capture its **actual** cost and wall-clock from the response, not a token
   estimate.
3. Extrapolate to the full run: `N items x per-unit cost`, plus expected retries,
   plus tool / web-search fees (which bill separately from tokens).

```python
import time
from litellm import completion, completion_cost   # keyed path; keyless: result.cost_usd

t0 = time.perf_counter()
resp = completion(model=WRITE_MODEL, messages=one_items_messages)
unit_cost = completion_cost(completion_response=resp)
unit_secs = time.perf_counter() - t0
# concurrency (below) means wall-clock is not N x unit_secs -- divide by the pool
# size for a fanned-out step, and add per-item tool/search fees on top of tokens.
print(f"~{n_items} items: ~${unit_cost * n_items:.2f}, ~{unit_secs * n_items / pool_size:.0f}s")
```

Report the number in the outline gate so the user approves the cost/time before
the full pipeline exists, rather than discovering it after the first real run.

## Concurrency

Both `completion` and the `claude_p_*` helpers are synchronous. A fanned-out step
(one call per item) should run its independent calls concurrently with a bounded
`concurrent.futures.ThreadPoolExecutor`, not in a serial loop. See the
"Optimize independent work" section of
`.agents/shared/worker/references/harden-artifact.md` for the general
parallelize/bound/deterministic-order contract; the AI-specific notes:

- **Bound the pool** (e.g. `max_workers=6`) -- an unbounded fan-out trips
  provider rate limits and is slower and worse than serial.
- **Collect and re-order results explicitly** (e.g. sort by item id) so output is
  deterministic and tests stay stable, rather than relying on completion order.

## Retries and partial failure

Model output is not guaranteed well-formed. Wrap the parse/validate in a small
retry loop, and isolate a failed item so one bad call does not sink the run:

- Retry 2-3 times on invalid JSON or a response that fails a structural check
  (e.g. a required marker missing), then give up on that item.
- Catch per-item exceptions inside the pool, keep the successes, and surface which
  items failed and why -- a partial result beats a crash (this is the
  tolerate-partial-failure contract from `harden-artifact.md` applied to model
  calls).

## Tool-use caps are a cost knob

If a call does use a tool loop (a server-side tool, or an agent's tools), the
number of tool uses is a first-class cost lever -- each turn re-feeds results as
input tokens and any search bills separately. Cap it (e.g. an Anthropic
server-tool's `max_uses`, or how many items you hand one agent) and treat the cap
as a tunable you measure, not a default you leave wide open.

## Keyed path: litellm vs the `anthropic` SDK for tool-using calls

For a **plain, no-tool** completion, litellm (the keyed path in `SKILL.md`
scenario 1) is correct and simplest. The trouble is specifically at the
intersection of **server-side tools (e.g. web search) + concurrency + a minimal
script env**:

- Passing Anthropic's `web_search_20250305` tool through litellm's `completion()`
  routes it through litellm's responses / MCP machinery
  (`litellm.responses.mcp.chat_completions_handler`), which does a **module-level
  `import fastapi`** via `litellm/proxy/openai_files_endpoints/common_utils.py`.
  `fastapi` ships only in the `litellm[proxy]` extra, not core -- so a script that
  depends on plain `litellm` dies with `No module named 'fastapi'`. This is a
  known litellm bug (BerriAI/litellm #18193, #13827).
- Under a thread pool those optional submodule imports are lazy and **not
  thread-safe**, so concurrent first-calls race and additionally fail with
  `cannot import name 'acompletion_with_mcp' from ...mcp.chat_completions_handler`.
  A solo call succeeds (nothing races); warming the import in the main thread does
  **not** help, because `fastapi` is genuinely absent.

Preferred fix: **don't pass a server-side tool at all** -- do web search with an
agent (`SKILL.md`, "Web search"), so the completion stays plain and litellm is
fine. If you genuinely must use a server-side tool from a script, use the
official **`anthropic` SDK** instead of litellm on that path: it is thread-safe,
supports `web_search_20250305` natively, and reads the base URL + key from the
environment with no wiring. (Installing the `litellm[proxy]` extra fixes the
missing-`fastapi` import but still leaves the concurrency raciness, so the SDK is
cleaner.)

## Base-URL wiring (proxy deployments)

A deployment may route Claude through a proxy advertised as `ANTHROPIC_BASE_URL`
(an Anthropic-Messages-API-compatible endpoint). The `anthropic` SDK reads
`ANTHROPIC_BASE_URL` and `ANTHROPIC_API_KEY` from the environment natively, so
`Anthropic()` with no arguments talks to the proxy out of the box. litellm reads
a **differently named** var (`ANTHROPIC_API_BASE`, not `ANTHROPIC_BASE_URL`) and
is sensitive to a trailing slash -- so on litellm you must pass
`api_base=os.environ["ANTHROPIC_BASE_URL"].rstrip("/")` explicitly. One more
reason the SDK is the smoother path when a script needs the proxy plus tools.
