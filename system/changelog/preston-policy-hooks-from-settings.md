This repo declares its own codex and pi guards, in the files those harnesses already read.

`.codex/hooks.json` gives codex the same scripts `.claude/settings.json` gives claude. Codex loads hooks from every active config layer without a higher layer replacing a lower one, so this file runs alongside the per-agent one mngr writes for its own bookkeeping hook. Its two preconditions -- a trusted project layer and trusted hooks -- are already met by the workspace trust mngr seeds and the `--dangerously-bypass-hook-trust` it passes.

`.pi/extensions/policy_guards.ts` does the same for pi, which has no shell-hook surface: pi auto-discovers extensions from `.pi/extensions/`, calls every extension's `tool_call` handler, and blocks when any returns `{block, reason}`. It spawns the same `agent_latchkey_request_check.py` and `agent_tk_standalone_check.py` the hook wrappers do, so one checker file serves all three harnesses.

Neither needed anything from mngr, which no longer carries a guard list for either harness.

`agent_tk_standalone_check.py` gained the `from __future__ import annotations` its sibling already had; without it the checker crashed on a python older than 3.10 rather than checking anything.
