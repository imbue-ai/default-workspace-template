The `engineering-subordinate` output style no longer teaches its lessons with
examples that imply a technical reader. Its rules are unchanged; the
illustrations for "lead with the payload", numbered steps, next actions, and
error reporting now show user-visible outcomes ("Login now works with magic
links") rather than shell commands and `file.ts:42` references. Error reporting
gains an explicit split: quote exact errors verbatim when the user asked for
technical detail or has to act on them, otherwise describe the failure in
plain terms. Confirmations before destructive actions are likewise to be
explained non-technically.

Review-gate instructions no longer name `/autofix` in four places
(`op-heal.md`, `op-update.md`, `build-app`, and `update-self`'s
worker-review-gates). Those files now defer to `harden-creation.md`, which is
the one place the gate sequence is described, so the details cannot drift out
of sync. `harden-creation.md` itself now tells the unattended run to go through
the fix loop **once** rather than iterating to exhaustion.
