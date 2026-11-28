# Review gates

The gates a worker runs before it reports its work done. Your own guide says
which command prints the test gate for your change, which commit is the tree
before your change, and where a flaky test goes in your report. The rest is
here.

1. Ensure all in-flight changes have settled and are committed
2. Run the test gate below and fix what it flags with narrowly targeted
   changes

## Contents

- The test gate
- A failing test
- A failing test you would change
- A suite the selector left out
- A regression the gate let through
- A command that dies from a signal

## The test gate

The gate is whatever your change can reach, and `app-manifest select-tests`
prints it, run from the repo root the way your guide says. The selector reads
commits: it refuses to run while the working tree holds an uncommitted or
untracked change, naming it, so commit everything first.

It prints shell lines, each under a comment naming the changed paths behind
it. Run every line, in order, each as its own command: they include the
frontend build the browser tests need, and passing two pytest roots to one
invocation breaks collection. What it selects, and why, is in
`system/libs/app_manifest/README.md` ("Selecting tests"): the repo guards, the
changed packages' and skills' own suites with their ratchets, the suites of
whatever declares a dependency on them, the browser tests too of an app whose
Python changed, only the `browser` and `frontend` tests of the chat app or the
shell when its frontend alone changed (any other app runs whole), each changed
test file of those two on its own, and the frontend checks. A changed path
outside every package and skill, or in a package the root project depends on,
brings in the full root suite; that is the gate working, not a gap. Do not
drop any line of it.

Memory pressure is never a reason to skip a command of the gate, or to report
`done` without it.

## A failing test

Rerun only the tests that failed (their node ids, from the same directory the
line ran in) before anything else. Add `--no-cov` when the line's run reported
coverage (the chat app's, the shell's and Getting Started's do): a run of only
some tests fails the coverage floor. In the chat app's or the shell's suite
add `-m ''` too, or a browser test named in it is deselected (AGENTS.md). A
test that passes on that rerun without any change of yours is flaky, not
broken: name it as flaky in your report, where your guide says (your lead
reports or tickets it), and move on, without rerunning its suite. Check the
shed ledger for its run first ("A command that dies from a signal"): a failure
the ledger shows was shed is a shed, handled as one, not a flaky test. Other
than a shed command's rerun, a suite runs whole again only after you commit a
fix, and then as whatever the fix reaches:
`select-tests --diff-base <the commit before the fix>`. Everything in that
range is your own commits, so the selector needs nothing else. Those lines are
that commit's whole gate; a suite they did not print is a widening, and goes
through the `question` gate like any other.

## A failing test you would change

Before editing a test that failed, run it on the tree before your change: with
everything committed, `git checkout --detach <the commit before your change>`
(your guide names it), rerun the test's node id the way the line ran it but
with `uv run --no-sync` (your branch's venv stays), then check your branch
back out. Say in your report whether your change caused the failure or it was
already there; a fix to a test that already failed is reported as a fix to a
pre-existing failure, not as part of your gate.

## A suite the selector left out

The selector reads only declarations: `pyproject.toml` and `package.json`
dependencies, `uv.lock`, the programs in supervisord blocks, and app
manifests' `[[references]]`. A coupling none of those shows is invisible to
it. When you have a concrete reason to think a suite it left out can observe
your change (it runs your script as a subprocess, calls your service over
HTTP, reads a file your change writes), run that suite too. When that suite is
an app the workspace built and the path lies outside the app, declare the
coupling as part of your change: add the path to the app's `app.toml` as a
`[[references]]` entry, so the next change to it selects the app without
anyone having to notice. When the suite is built-in, leave its declarations
alone: a local edit to a built-in file only makes divergence for the next
update, and a reference would widen a built-in app's footprint into other
units' files. Such a coupling is a built-in defect (AGENTS.md, "Updates"):
name the suite and the path it observes in your `done` report under
`Undeclared couplings:`, and your lead includes it in its report of built-in
issues for the pass.

## A regression the gate let through

When your task fixes something an earlier change broke, and a test catches the
break (one that already existed, or the one you add), check whether the
selector picks that test for the earlier change:

```bash
uv run --no-sync app-manifest select-tests --diff-base <breaking commit>^ --diff-ref <breaking commit>
```

If the test's suite is not among the lines, declare the coupling that would
have selected it, the same way.

## A command that dies from a signal

Exit status 137 or 143, or `Killed` with no failure output, is not a test
failure until the shed ledger says it is not a shed. Note the time before each
command (`date -u +%Y-%m-%dT%H:%M:%S`), and on a signal death look for sheds
since then, with the time you noted written into the lookup (a shell variable
does not survive from one command to the next):

```bash
STARTED_AT=2026-09-24T11:00:00  # the time you noted before the command
jq -c --arg since "$STARTED_AT" \
    'select(.type == "process_shed" and .timestamp >= $since)' \
    /home/user/workspace/data/.state/oom_priority/events/shed.jsonl
```

A record whose `pid` or `comm` is the command or one of its children (pytest,
an xdist worker, node, a browser) means the command was shed for memory
pressure: that is a shed, never a failure to fix in the code. A shed child can
also surface as ordinary test errors (a browser that vanished mid-test), so
run the same check when failures look unrelated to your change. Then judge
whether a rerun would be shed again, from the ledger (are shed records still
arriving, for processes other than yours?) and from the free memory the guard
acts on,
`python3 system/services/oom_priority/bin/workspace_memory.py free`
(is it recovering since the shed, or still falling?). Not `/proc/meminfo` or
`free`: under a memory cap they read far more free memory than the guard
sees. If things are settling, rerun the shed command. If they are not, or the
rerun is shed too, ask your lead with a `question` gate naming the command,
the ledger records, and the free memory you saw; your lead frees memory with
the user, and answers when you can rerun.
