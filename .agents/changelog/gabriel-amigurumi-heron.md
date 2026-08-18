The `update-system-interface` reveal now reports *why* a merged backend failed
its pre-flight check. The throwaway boot's stdout and stderr were being sent to
`/dev/null`, so a failed reveal said only "merged backend failed to boot in a
pre-flight check" -- with the process and its output already gone, nobody could
tell a genuinely broken backend from a slow one, and diagnosing it meant
guessing. The boot's output is now captured and the tail of it rides back on the
failure message and into the auto-revert commit, so the reason survives in git
history.

The pre-flight also stops polling as soon as the throwaway backend has exited: a
backend that died on import will never turn healthy, and waiting out the rest of
the 30-second deadline only delayed the rollback.
