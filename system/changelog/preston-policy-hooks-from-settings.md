This repo now declares its own policy guards for codex and pi, instead of mngr carrying the list.

`[agent_types.codex.policy_hooks]` in `.mngr/settings.toml` maps each codex hook event to the commands to run for it -- the same scripts, in the same shape, that `.claude/settings.json` gives claude. `[[agent_types.pi-coding.policy_checkers]]` declares the two checkers pi runs before a bash tool call, each with a `match` pre-filter.

mngr used to name these scripts itself: `build_codex_hooks_config()` listed six of them by filename, and pi's lifecycle extension spawned two by path. Adding a guard therefore meant a mngr release, and any other workspace using those harnesses inherited hooks it never asked for. Now the guard set lives with the guards.

`POLICY_HOOKS.md` says where each harness's wiring lives, and the "keeping the three in step" list points at the settings tables rather than at mngr internals.
