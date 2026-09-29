A build's plan-node workers now run on the model their node's difficulty calls for
rather than all on Opus. The planner already assigned every node a `capability`
(`low`, `medium`, `high`, or `interactive`); `plan_orchestration.py` turns that into
the per-node `model` in `plan.json`, which the orchestrator passes to `mngr create`.
The mapping is `high` to Opus 5.5, `medium` to Sonnet, `low` to Haiku 4.5.

Each tier names a Claude Code alias (`opus[1m]`, `sonnet[1m]`, `haiku`) rather than a
dated model id, so a tier follows the pinned binary's own alias table to the current
best model in its family: `sonnet[1m]` is Sonnet 5 on the pinned 2.1.280 and becomes
Sonnet 5.5 whenever a pin carries that model, with no edit to the table.

The `[1m]` suffix is a fix, not decoration. The previous table said plain `opus`, and
a settings override outranks the repo's own `.claude/settings.json` -- so every worker
had been running without the 1M context window the workspace provisions, invisibly,
since the bare alias is accepted and reports the same display name.

`BUILD_APP_WORKER_MODEL=opus[1m]` puts every worker back on one model, which is the
arm any comparison of the tiering has to run against. It is read when `plan.json` is
written, so `plan.json` stays a record of what each node actually ran on.
