New acceptance test (`harnesses/claude/test_harness_contract.py`) that exercises the model bar's live-read chain end to end against the REAL pinned Claude Code binary.

That chain has three links -- Claude Code's statusline payload, `system/scripts/claude_status_line.sh` selecting four fields out of it, and `match_option` resolving the result against `CLAUDE_CATALOG` -- and every existing test of it drives a FROZEN payload capture. A frozen capture cannot notice that the binary changed, so an upgrade can invalidate the whole chain while the suite stays green, and the bar silently shows nothing. The reported model ids are exactly the kind of value an upgrade moves.

The test asserts the ids a live claude actually reports resolve to the options the picker offers -- both for the model the workspace launches (`opus[1m]`) and for each model reachable by `/model`. Run against the catalog as it stood before the Opus 5 fix, it fails with `the live reported model id 'claude-opus-5[1m]' matches NO catalog option`, which is precisely the blank-model-bar bug.

It needs no credentials and runs no model turn: claude writes its statusline before it ever calls the API, so a syntactically-valid but non-functional key reaches the whole chain. Fast mode is deliberately not asserted -- `/fast on` is a no-op under an unusable key, and the reported value is not stable across a real turn.

CI now installs tmux and the pinned Claude Code before the system_interface suite, reading the version from `[agent_types.claude].version` rather than repeating it, so the test actually runs instead of skipping.
