- **`harden-worker` is a skill.** It moved from `.agents/shared/worker/` to `.agents/skills/harden-worker/`, with its references. Its description says it is not for a chat to invoke. The task files `crystallize-creation`, `heal-creation` and `update-creation` write tell the worker to invoke the `harden-worker` skill. Every pointer to the old path follows.

- **Deleted.** The `dealing-with-the-unexpected` skill; the Co-Authored-By trailer in `submit-upstream-changes`; the pointer to `submit-upstream-changes` in `launch-task`'s dead-worker reference, which now says to merge the branch; the review stages `lead-proxy.md` named that no longer run.

- **Small corrections.** `notify-user`'s `compatibility` line matches how it runs. `do-something-new` no longer names a skill to the user. `connect-external-service` states the secret-card procedure once, in `direct-api.md`, and drops its hard-coded service list in favour of `latchkey services list`. Stale citations and terms are fixed in `launch-task` and `check-app-errors`.

- **`validate_skill.py`.** A shared file's relative path is no longer resolved against the skill that reads it, and a linked script that imports the skill's package reaches the skill's `python/` project.
