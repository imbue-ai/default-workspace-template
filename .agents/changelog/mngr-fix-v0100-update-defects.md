- `update-self` no longer leaves a raised launch-rule count behind. The migration reference has a new "Launches and import-path edits in the user's code" section, and the worker guide tells the update to take upstream's `system/test_python_launch_rules.py` (conflict or not) and fix the workspace's own launches and import-path edits by restructuring them, never by raising a count. Both docs say that restructuring is part of the update's job, even when it reaches code the update did not otherwise touch.

- `running-python.md` documents the `# own-process:` and `# import-path:` markers for a launch or import-path edit that really has to stay, and adds that a deadline is a reason for a process only when the work can hang where no timeout reaches.

- `user-package-migration migrate` gives a package whose tests type-check it (`check_no_type_errors`) the root's `ty` pin in its dev group and a `[tool.ty]` table, as build-app's scaffold does. Without them, a migrated app's type check walked up to the root project and failed on every built-in skill's imports.

- Every documented wait through `run_in_background.py` (`create_worker.py await` in launch-task, update-self, crystallize-creation, heal-creation, update-creation, migrate-workspace, publish-template and the lead-proxy reference; the GitHub-approval waits; datalib's sync wait) passes the new `--waiter` flag.
