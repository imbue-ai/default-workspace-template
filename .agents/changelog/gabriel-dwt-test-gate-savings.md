Harden workers and update-self now run only the tests a change can reach, as `app-manifest select-tests` prints them, instead of a fixed set that ran the repo guards twice, fell back to the whole root suite whenever a change left the creation's footprint, and never collected the shell.

- A worker's test gate is `select-tests --diff-base "$DIFF_BASE"`, run line by line. Every harden task now carries a `diff_base`, services and the shell included. The selector refuses to run while the worker has an uncommitted or untracked change, so no edit escapes the selection. A full root suite it prints for a path outside every package and skill, or in a package the root project depends on, is the gate working, not a gap to map.

- A worker that has a concrete reason to think a suite the selector left out can observe its change runs that suite too, and, when the suite is an app the workspace built, declares the path in the app's `[[references]]`. A heal that fixes a regression checks whether the selector would have picked the catching test for the change that caused it, and declares the missing coupling when it would not. A built-in suite's coupling is never declared locally: the worker names the suite and the path it observes in its report under `Undeclared couplings:`, which the lead adds to its single report of built-in issues.

- A gate command that dies from a signal is checked against the shed ledger. A shed is rerun once memory is settling, or raised to the lead as a `question`, and is never skipped or treated as a test failure.

- When an agent reports a shed, the lead follows the new `shared/references/freeing-memory.md`: list what could be stopped, offer it to the user, and stop only what they approve.

- Update-self's validation step runs the selector over the merged files, the update's changes inside every creation that carries local content, the creations the impact analysis found (listed in `impacted-paths.txt`), and the worker's own edits.

- The critical-app handoff says which commit the harden task's `diff_base` is: the one the branch forked from the served tree.
