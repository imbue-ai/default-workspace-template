- **Python floor:** every skill's scripts are type-checked by the root project, and the skill projects require Python 3.12.

- **update-self on an older Python:** the staged `update_self.py` now checks the interpreter before importing anything. On a workspace whose system Python is older than 3.12 (a local workspace from before 2026-09-14), it exits with code 75 and says the update is impossible. Step 3a then treats that as a migration-required update and offers `migrate-workspace`.

- **Migration reference:** a new section on fixing ty errors in a workspace's own scripts.
