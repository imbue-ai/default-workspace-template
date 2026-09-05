# Verification

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

When `type` is app, skill, or service, `{creation_context}` in the invocations
below is this paragraph, pasted verbatim:

    The creation is a user's own and lives only in this workspace. Judge it
    against the conventions for its type (`type-<TYPE>.md`,
    `system/apps/README.md`), not against `system_interface`'s patterns, and
    do not flag portability to environments the creation will never run in.

### Verify Architecture

Run architecture verification before autofix.

    /verify-architecture Run fully unattended: never call AskUserQuestion.
    In Phase 3, pass the analysis agent the creation context verbatim
    alongside the problem description: {creation_context}

### Autofix

Autofix's normal final step asks the user to keep or revert each proposed fix
via AskUserQuestion, which is unavailable in a worker -- so split that decision
out and make it yourself. Invoke autofix so it *applies* its fixes but leaves
the keep/revert judgment to you:

    /autofix Run fully unattended: never call AskUserQuestion. Run the fix
    loop a single time, not 10 times. Leave every fix commit applied, and
    report the fix commits (hash + full message). Do not revert anything yourself
    -- the caller will decide. Include this context in the description you
    pass to agents: {creation_context}

Then review those fix commits against what this branch is meant to do. You hold
the task context the fix subagents run without, so you are the right judge of
whether each fix is correct. Keep fixes by default; revert only the ones that
undo intended behavior or are otherwise wrong (`git revert --no-edit <hash>`,
newest first). Record which you kept and which you reverted in your gate report.