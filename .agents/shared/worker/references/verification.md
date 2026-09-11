# Verification

Run the repo's review gates -- `/verify-architecture` and `/autofix` -- and
fix what they flag **before** writing the final gate report, so the user sees
a single report that already reflects the review verdicts rather than a
report-then-verify-then-report-again pattern.

The gates are part of the harden contract, not a step you may adapt. Run them
as written unless the operation's own reference defines an explicit skip
condition (as `update-self` does for a pure clean pull) and you can show its
conditions hold. You are not permitted to skip gates or narrow them, even with full
disclosure in your report. If you believe a gate should not run, or should run
at another scope, in a situation no skip condition covers, surface that as a
mid-flight `question` gate and stop -- `question` is valid on every run,
whatever gate names your operation reference lists. If you do not ask, run the
gates as written and record your reasoning in the report for the lead to weigh:
the fallback is always more coverage, never less. A scoped-down or hand-rolled
substitute reported as "review" is worse than no gate at all, because it reads as
coverage that does not exist.

## Pin the review base first

Each gate resolves its own diff base in its first phase, from
`stop_hook.base_branch` with the precedence
`$CODE_GUARDIAN_STOP_HOOK__BASE_BRANCH`, then `.reviewer/settings.local.json`,
then `.reviewer/settings.json`, then `main`. The committed `settings.json` says
`main`, and `main` is never a safe base for a harden pass: if your lead has
provisionally merged your milestone, `main...HEAD` no longer covers your work
and is usually empty -- at which point both gates are instructed to stop and
ask the user which base is right, which a worker cannot do. Asking the reviewer
in prose to use a different base is not enough; the base is resolved before
`$ARGUMENTS` is read.

So pin it before you invoke either gate. `settings.local.json` is gitignored
and wins over the committed file, so this stays local to your worktree and out
of your diff:

```bash
python3 - "$SCOPE_FILE" <<'PY'
import json, pathlib, sys
base = json.loads(pathlib.Path(sys.argv[1]).read_text())["diff"]["base"]
path = pathlib.Path(".reviewer/settings.local.json")
settings = json.loads(path.read_text()) if path.exists() else {}
settings.setdefault("stop_hook", {})["base_branch"] = base
path.write_text(json.dumps(settings, indent=4) + "\n")
print(f"pinned review base: {base}")
PY
```

Then confirm the range is non-empty before running anything:

```bash
git diff --name-only \
    "$(jq -r .stop_hook.base_branch .reviewer/settings.local.json)"...HEAD
```

An empty range means `diff_base` is wrong for this pass. Stop and report it
rather than running the gates: a gate over an empty diff reports success
having reviewed nothing, which is worse than not running it.

## The scope brief

Both invocations carry the run's footprint into the review agents through
`$ARGUMENTS`, which each gate skill forwards verbatim into its sub-agent brief.
When `type` is app, skill, or service, `{scope_brief}` below is this block,
pasted verbatim with `<SCOPE_FILE>` replaced by the scope file's path
(`data/.tasks/harden/<slug>/scope.json`, per `harden-creation.md`) and
`<REVIEW_BASE>` by that file's `diff.base`, which is the `diff_base` your task
frontmatter pinned at dispatch:

    The creation is a user's own and lives only in this workspace. Judge it
    against the conventions listed in the scope file below, not against
    `system_interface`'s patterns, and do not flag portability to environments
    the creation will never run in.

    Review the range <REVIEW_BASE>..HEAD. Do not derive the diff from `main`
    or any other branch name: the lead may have provisionally merged a
    milestone from this branch into `main` while you were working, which makes
    `main...HEAD` wrong -- often empty. <REVIEW_BASE> is authoritative, and a
    disagreement between it and `main` is expected rather than a finding.

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
    Review <REVIEW_BASE>..HEAD; the configured `base_branch` is already
    pinned to that commit. In Phase 3, pass the analysis agent this scope
    brief verbatim alongside the problem description: {scope_brief}

### Autofix

Autofix's normal final step asks the user to keep or revert each proposed fix
via AskUserQuestion, which is unavailable in a worker -- so split that decision
out and make it yourself. Invoke autofix so it *applies* its fixes but leaves
the keep/revert judgment to you:

    /autofix Run fully unattended: never call AskUserQuestion. Review
    <REVIEW_BASE>..HEAD; the configured `base_branch` is already pinned to
    that commit. Run the fix loop a single time, not 10 times. Leave every fix
    commit applied, and report the fix commits (hash + full message). Do not
    revert anything yourself -- the caller will decide. Include this scope
    brief verbatim in the description you pass to agents: {scope_brief}

Then review those fix commits against what this branch is meant to do. You hold
the task context the fix subagents run without, so you are the right judge of
whether each fix is correct. Keep fixes by default; revert only the ones that
undo intended behavior or are otherwise wrong (`git revert --no-edit <hash>`,
newest first). Record which you kept and which you reverted in your gate report.
