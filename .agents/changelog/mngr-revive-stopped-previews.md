- `preview_app.py revive --app <name>` boots a preview a workspace restart stopped again, by re-running its last `up` (same worktree, siblings, instance key, and title, over a fresh copy of the live data). `mock_frame.py revive --app <name>` does the same for a UI sketch's frame. Both leave a preview or frame that still runs alone. The update-app skill gives agents the preview's command.

- Booting an instance again (`serve_isolated_instance.py up` over an earlier one, or a re-frame) now updates the rows its window shows in place instead of removing and re-adding them, and a boot that fails keeps those rows recorded, so `down` still removes them and a later boot can try again.

- `up` now records each process's start time, so `down`, `refresh`, and a re-`up` never signal a process that a restart handed the same pid, and a revive never takes a pid a restart reused for the preview still running.

- An `up` or `refresh` whose server exits before it answers (a build that crashes on start) fails as soon as the process is gone, instead of after the full minute-long health wait.

- The row a preview's or UI sketch's window shows now names the app it previews or frames, its kind, and the chat that booted it (`isolated_instance`), which is how the shell offers **Start it again** in the window and drafts **Ask in chat** into that chat. `preview_app.py up` and `mock_frame.py up` record the chat of the agent that runs them (`MINDS_CHAT_ID`, else `MNGR_AGENT_ID`), and a revive keeps the recorded one.
