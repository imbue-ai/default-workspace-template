Harden workers and update-self now run only the tests a change can reach, as `app-manifest select-tests` prints them, instead of a fixed set that ran the repo guards twice, fell back to the whole root suite whenever a change left the creation's footprint, and never collected the shell.

- A worker's test gate is `select-tests --diff-base "$DIFF_BASE"`, run line by line. Every harden task now carries a `diff_base`, services and the shell included.

- A path the selector cannot classify still brings in the full root suite. The worker records a mapping for it in the override file, and names a built-in one under `Selector gaps:` in its report, which the lead adds to its single report of built-in issues.

- A gate command that dies from a signal is checked against the shed ledger. A shed is rerun once memory is settling, or raised to the lead as a `question`, and is never skipped or treated as a test failure.

- When an agent reports a shed, the lead follows the new `shared/references/freeing-memory.md`: list what could be stopped, offer it to the user, and stop only what they approve.

- Update-self's validation step runs the selector over the merged files plus the worker's own edits.
