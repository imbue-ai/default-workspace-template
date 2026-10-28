AGENTS.md now points a chat's test gate at `app-manifest run-tests`, which runs the commands and says how each ended.

CI no longer remounts `/tmp` `noexec` before the Python suites: no workspace mounts it that way, and pytest-executable-tmp no longer has a fallback to exercise.
