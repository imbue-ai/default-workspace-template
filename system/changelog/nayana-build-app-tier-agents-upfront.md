The three per-difficulty agents now start in Step 1, beside the integration folder, instead of
when the first node of their difficulty is ready. Nothing about them depends on the plan: the
difficulties are always `low`, `medium` and `high`, and their models come from the one table in
`plan_orchestration.py`, which a new `models` subcommand prints so the skill does not repeat it.

Their first task is `references/tier-agent-priming.md`: read `worker-node.md`,
`app-building-guidance.md`, `worker-reporting.md` and the scaffolder, write a one-line ready
report, and wait. That is the reading a fresh worker did at the start of every node -- 144 seconds
over 8 files in one measured build, 24 more in its sibling -- done once, during the clarifying and
the planning, where the orchestrator was idle anyway.

Step 4 no longer launches anything for a tier agent: every node is a message to an agent that is
already warm.
