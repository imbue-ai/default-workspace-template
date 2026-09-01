`build-app` now starts by firing off a plan recorder and forgetting it: a
single opaque command (`system/scripts/imbue_plan_extra/write_plan.sh
"<request>"`) that returns immediately and records, for offline analysis, a
routing plan for the same request. The skill is explicit that this is not part
of building the app -- no step record for it, no mention to the user, no
waiting on it, no reading its output, and failures ignored.

Nothing in the workspace reads those plans back. They land in
`data/.imbue/plans/`, which ships with a `CLAUDE.md` telling every agent never
to read, search, summarise, or act on anything in it, and every plan file
carries a fixed header saying the same.
