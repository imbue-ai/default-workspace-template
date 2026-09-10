# Verification

No run loads this file: the two gates below are parked in the harden pass, so
`harden-creation.md` does not list it. It is kept current so the invocations are
correct the day they are run by hand or re-wired.

Run the repo's review gates -- `/verify-architecture` and `/autofix` -- and
fix what they flag **before** writing the final gate report, so the user sees
a single report that already reflects the review verdicts rather than a
report-then-verify-then-report-again pattern.

The gates are part of the harden contract, not a step you may adapt. Run them
as written unless the operation's own reference defines an explicit skip
condition (as `update-self` does for a pure clean pull) and you can show its
conditions hold. You are not permitted to skip gates or narrow them, even with full
disclosure in your report. Where your operation reference defines a mid-flight
gate for it (`update-self`'s `question`), surface it there and stop. Where it
defines none -- the crystallize / update / heal enums are stage-bound approval
gates, and `final-creation` does not fire until after the gates -- run the gates
as written and record your reasoning in the report for the lead to weigh: the
fallback is always more coverage, never less. A scoped-down or hand-rolled
substitute reported as "review" is worse than no gate at all, because it reads as
coverage that does not exist.

## The scope brief

Both invocations carry the run's footprint into the review agents through
`$ARGUMENTS`, which each gate skill forwards verbatim into its sub-agent brief.
When `type` is app, skill, or service, `{scope_brief}` below is this block,
pasted verbatim with `<SCOPE_FILE>` replaced by the scope file's path
(`data/.tasks/harden/<slug>/scope.json`, per `harden-creation.md`):

    The creation is a user's own and lives only in this workspace. Judge it
    against the conventions listed in the scope file below, not against
    `system_interface`'s patterns, and do not flag portability to environments
    the creation will never run in.

    The creation's footprint is declared in <SCOPE_FILE>. Read, in this order:
    the diff; every file under `primary`; the `wiring` sections named there, in
    the files they name; every `references` path -- for a skill reference, its
    `SKILL.md` and its `scripts/`, so you see how it invokes the app; every
    `context` path, read-only; the `conventions` list.

    Never read a path matching `exclude`; the scope file itself is the one
    exception.

    Do not read other apps, or any path outside the footprint, unless you are
    following a concrete import, call, URL, or file path from inside the
    footprint, or a finding cannot be confirmed without it.

    List every file you read outside the footprint and the conventions under
    `Expanded to:` in your report, with the reason for each.

    Check the consumer contract in both directions: a change to the app's
    routes, CLI, or stored data shape has to be reflected in every `references`
    entry that uses that surface, and a change inside a referenced skill,
    script, or doc has to match the app's current surface. Flag any drift.

### Verify Architecture

Run architecture verification before autofix.

    /verify-architecture Run fully unattended: never call AskUserQuestion.
    In Phase 3, pass the analysis agent this scope brief verbatim alongside
    the problem description: {scope_brief}

### Autofix

Autofix's normal final step asks the user to keep or revert each proposed fix
via AskUserQuestion, which is unavailable in a worker -- so split that decision
out and make it yourself. Invoke autofix so it *applies* its fixes but leaves
the keep/revert judgment to you:

    /autofix Run fully unattended: never call AskUserQuestion. Run the fix
    loop a single time, not 10 times. Leave every fix commit applied, and
    report the fix commits (hash + full message). Do not revert anything yourself
    -- the caller will decide. Include this scope brief verbatim in the
    description you pass to agents: {scope_brief}

Then review those fix commits against what this branch is meant to do. You hold
the task context the fix subagents run without, so you are the right judge of
whether each fix is correct. Keep fixes by default; revert only the ones that
undo intended behavior or are otherwise wrong (`git revert --no-edit <hash>`,
newest first). Record which you kept and which you reverted in your gate report.
