Documented the new `# CLEANUP:` comment convention in `style_guide.md` (with a
pointer in `CLAUDE.md`): rollout-bridging code -- compatibility shims,
migration guards, temporary probes -- is marked with a `# CLEANUP:` comment
that states both what can be cleaned up and when it becomes safe to do so, so
rollout debt is greppable and removable after each deploy.

Added a `just backfill-autostart` recipe wrapping the new
`minds server backfill-autostart` sweep (the reboot-resilience rollout's
in-VM backfill).
