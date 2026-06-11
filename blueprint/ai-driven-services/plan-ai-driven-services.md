# AI-driven services

How a service in this repo calls Claude. The goal is the smallest possible
surface: no bespoke "AI integration" library, no routing layer, no spend
tracking. A service that needs Claude either calls `litellm` directly (when an
API key is present) or shells out to `claude -p` (when it isn't), and the
`use-ai-integration` skill teaches the building agent which to do.

## Overview

- A service reaches Claude in one of two ways, chosen by the *building agent* at
  the time it writes the service -- not by a runtime router. The choice is
  driven entirely by whether `ANTHROPIC_API_KEY` is set in the service's
  environment.
- **Keyed:** the agent writes `litellm` directly (added to the root
  `pyproject.toml` as a dependency; the agent reads litellm's docs as needed).
  Nothing is wrapped or abstracted on our side.
- **Keyless:** the agent copies a small, self-contained `claude -p` -> JSON
  helper that the skill ships as a reference snippet. The snippet handles the two
  things that are easy to get wrong by hand: unsetting `MAIN_CLAUDE_SESSION_ID`
  (so the spawned `claude -p` doesn't trip mngr's session hooks) and
  distinguishing the success vs. error arms of the `claude -p` JSON result.
- Three levels of agency are still recognized -- a plain completion, a one-shot
  agentic task, and a full launched agent -- but none of them is a library
  function. The first two are the same `claude -p` snippet used with different
  flags; the third is a direct call to the existing
  `launch-task`/`create_worker.py launch-sync` path.
- Cost is surfaced, not tracked. `claude -p` reports the actual cost of each
  call; the skill teaches the agent to reprice that call's token usage against
  litellm's own price data to show a concrete "add a key and save ~$X" figure.
  There is no spend ceiling, no persisted ledger, and no price table we
  maintain.
- The motivation is scope reduction: the prior design carried a full
  `libs/ai_integration` package (keyed/keyless routing, two backends, a
  credentials module, a spend tracker, and a per-model price table) whose
  complexity was dominated by the keyless fallback and spend control. Collapsing
  to "litellm if keyed, a copyable `claude -p` snippet if not" removes almost
  all of that code while keeping the capabilities that matter.

## Expected behavior

- A developer (or agent) building a service that needs Claude consults the
  `use-ai-integration` skill, checks whether `ANTHROPIC_API_KEY` is set, and
  implements the appropriate path. There is no single entry point they import
  that hides this decision.
- **Keyed path:** the service calls `litellm` for a non-agentic completion. It
  gets back text plus token usage and a per-call cost from litellm. Structured
  output, tools, temperature, model choice, etc. are whatever litellm exposes --
  we add nothing on top.
- **Keyless path:** the service runs `claude -p` via the copied helper and gets
  back a small typed result carrying the response text, the reported
  `cost_usd`, the token `usage`, and the raw JSON. The helper:
  - unsets `MAIN_CLAUDE_SESSION_ID` in the child environment;
  - for a non-agentic completion, disables tools and runs from an isolated
    working directory so the repo's `CLAUDE.md` / `.claude` hooks can't hijack
    the answer;
  - for a one-shot agentic task, leaves tools enabled and runs in the repo so
    the agent can read/write files (relying on `bypassPermissions`, since a
    headless run has no human to approve tool use);
  - raises loudly on a `claude -p` error result (e.g. max-turns) or malformed
    JSON, rather than silently returning empty text.
- **Full-agent path:** for the rare case that warrants a full, possibly
  long-running agent (user- or error-triggered, tightly scoped -- never an
  autonomous loop), the service invokes `create_worker.py launch-sync`
  directly to launch, await a finish report, collect a structured result, and
  tear the agent down.
- **Cost / onramp:** a keyless service can report what each call actually cost
  and, using litellm's price data, what the same call would cost with a key --
  so the user can decide when volume justifies setting `ANTHROPIC_API_KEY`. No
  budget is enforced; if the user wants a ceiling they build it themselves.
- **Credentialing:** a deployed mngr agent normally has both
  `CLAUDE_CONFIG_DIR` and (usually) `ANTHROPIC_API_KEY` in its environment, so
  both paths "just work." A service with neither fails with a clear error from
  the path it attempted, not an opaque auth failure.
- **Billing isolation (unchanged reality, still documented):** `claude -p` and
  the direct API draw separate pools from the user's interactive chat, so heavy
  service usage never competes with the chat quota -- the live concern is cost,
  not chat availability. The footgun remains: with `ANTHROPIC_API_KEY` set,
  `claude -p` bills full API rates against the API account, so an unattended
  keyed `claude -p` loop can run up real spend.

## Changes

- **Delete the `libs/ai_integration` package entirely** -- the keyed/keyless
  routing layer, both completion backends, the credentials module, the spend
  tracker, and the per-model price table all go away.
- **Add `litellm` to the root `pyproject.toml`** as the supported library for
  the keyed path.
- **Rewrite the `use-ai-integration` skill** so it is purely instructional:
  - how to detect a key (`os.environ.get("ANTHROPIC_API_KEY")`) and branch;
  - when to use each of the three agency levels;
  - the keyed path via litellm, including how to read litellm's reported cost;
  - the keyless `claude -p` snippet and when each flag set applies;
  - the cost/onramp nudge (reprice `usage` via litellm's price data to estimate
    the savings a key would unlock);
  - the slimmed billing/credentialing guidance and the `ANTHROPIC_API_KEY`
    footgun -- minus everything tied to the removed library (routing, spend).
- **Ship the `claude -p` helper as a copyable reference snippet** under the
  skill (a real, syntax-valid `.py` reference file the agent copies/adapts into
  its service, not an importable package). It returns a small typed result
  (text, `cost_usd`, `usage`, raw) and supports the completion vs. agentic-task
  flag sets described above.
- **Keep the `launch-task` synchronous path** (`create_worker.py launch-sync`
  and its `destroy` support) -- it is the full-agent path. These changes come
  over from the existing branch as-is; the skill points at them rather than
  wrapping them.

## Notes / accepted trade-offs

- **Snippet drift (accepted):** because the `claude -p` helper is copied rather
  than imported, a future change to `claude -p`'s JSON shape or the
  session-hook fix won't propagate to services that already copied it. This was
  chosen deliberately to avoid maintaining a library; if drift becomes painful,
  promoting the snippet to a one-module `libs/` helper is the obvious escape
  hatch.
- **From-scratch framing:** this document describes the desired end state, not a
  diff. Deleting the prior `libs/ai_integration` implementation is called out in
  Changes because it currently exists, but the design here stands on its own.
