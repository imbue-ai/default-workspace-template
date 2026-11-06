An update-self pass can now run start to finish with nobody watching, started by the Imbue Studio app's in-workspace schedule or "Update now".

- **Runs without the app.** When the app's message pins the target and ceiling, the pass uses them and never asks the app for its version. The lead may clear the way without asking: pausing other chats and background agents, stopping the browser and windowless apps, pruning dead workers' worktrees, unused apply snapshots and tool caches, saving a dirty tree as one work-in-progress commit, and taking over an abandoned "updating workspace" lease. The results message names each such action with its undo.

- **Chats are paused, not waited on.** Right before the apply, `run-status stop-chats` pauses busy chats and background workers and saves their work; afterwards `run-status resume-chats` starts each again with a "Workspace updated" note. The run record gains `started_by` and `stopped_agents`.

- **New `NEEDS_DECISION` verdict.** A customization the update cannot keep ends the pass, with the worker stopped and its branch kept, instead of waiting; the user's answer in the chat, whenever it comes, resumes the kept pass.

- **Verdicts mean what they say.** `STUCK` is only for a fault the pass cannot recover from: a dead worker is restarted first (in every lead, per `dead-worker-recovery.md`), an app a pinned pass cannot reach is not a fault, and an apply that ends in an emergency records `STUCK`. `references/apply-outcomes.md` says when each verdict is recorded.

- **Survives restarts and stops on request.** A lead restarted by the app's watchdog (a workspace restart stops every chat) finds where the pass got to and carries on. A request to stop ends the pass `REFUSED`, with the worker's branch kept, leases released and paused chats brought back.

- **Rollbacks and critical apps.** A rolled-back apply's retry goes back to the same worker, which catches up, reverts the rollback on its branch and reports again (worker guide §8). A user's critical app whose probe fails blocks the update instead of being patched by the lead.

- update-self's `SKILL.md` is slimmer, with resuming, stopping and migration in references of their own; Step 6 shows the update chat in an existing window rather than opening a new one; and `manage-routines` says never to make a routine that runs update-self or the app's update dispatcher.
