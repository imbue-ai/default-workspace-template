# The version ceiling

The default update target **is** the release the **Mind app driving this
workspace** was built against, which `resolve-target` reads from the app itself
(`GET /api/v1/app/version` through the latchkey gateway; `ceiling` in the
output). The template carries the code the app talks to -- the system interface
and `mngr` -- so only that pairing was ever verified: a newer template speaks a
protocol the app does not know, and an older one is a release nobody shipped
with this app.

Three things make that release unavailable, and all three are **faults** rather
than refusals -- `resolve-target` fails, and what to do next is your call with
the user, not a version the script picks:

- the app cannot be reached, or is too old to report a version at all;
- it names a release the upstream does not carry, which means that release was
  never published as the app claims;
- it names no release at all (a dev build reports its branch), so there is
  nothing to match.

In the last case an operator testing a dev build knows which ref they want:
take it from them and pass it as `--override`. Never choose one for them.

Releases above the ceiling are treated as if they do not exist: never name one
the user did not ask for by name, or suggest updating the app to reach it. The
Mind app announces its own updates, on the user's release channel. A version
the user does name is an override, covered below.

## At the ceiling vs behind it

A workspace already sitting *at* the app's release gets a refusal rather than a
pass: the target is the release it is already on, so there is nothing to merge,
and `resolve-target` says so instead of spending a backup, a worker and a
validation run on a no-op. A workspace *behind* it still updates to it. The two
are distinguished by whether the resolved ref is already an ancestor of `HEAD`.

## Overrides past the ceiling

`"exceeds_ceiling": true` means the user's `--override` names a version this
app cannot vouch for -- newer than the app, or a branch or commit whose version
cannot be compared. Do not dispatch the worker on it silently. Tell the user
plainly what they asked for and what it risks ("that version is newer than your
Mind app, so parts of your workspace may stop working until you update the
app itself") and get an explicit go-ahead. This is the one confirmation the
otherwise-unattended flow keeps: it fires at launch, while the user is present,
and asks whether to *attempt* an unsupported version at all -- a question no
later rollback offer can substitute for. An override at or below the ceiling
needs no confirmation.

The message that started the pass may carry that confirmation already: a
launch that says the version is the user's explicit override, chosen knowingly
and not to be re-confirmed, is the same question answered up front. Treat it as
the go-ahead and do not put it to them a second time in chat; the rollback
offer after the apply stands as usual.

If the user declines, record `run-status verdict REFUSED --detail "<the version
they asked for, and that they chose not to attempt it>"` and end the pass.

## Why Step 3a re-checks from the staged copy

Step 2 runs from this workspace's *local* skill copy, and a workspace whose
template predates the ceiling has a local copy that does not check one: it
happily resolves the newest tag upstream, which is exactly the too-new target
the ceiling exists to refuse. The staged copy is by construction at least as
new as `$REF`, so 3a's check runs no matter how stale the initiator was. Keep
3a in any future version of the skill: it, not Step 2, protects a workspace
arriving from an older template.

3a resolves nothing: it either clears the target Step 2 chose or hands the pass
back to 2a with the ceiling's answer. When `$REF` becomes the capped ref --
silently, because the user never named the release Step 2 chose, or by the
user's choice over their own override -- §2a must be re-run for it before
dispatching: §2a staged the skill at the *old* `$REF`, and the staged copy
supplies the worker guide, the `update_self.py` both agents run, and the prose
the lead is reading -- leaving it in place would run the too-new release's flow
against a target that is not it. `bootstrap-skill` re-stages destructively, so
re-running it is safe, and 2a's `differs` branch then decides which document to
follow, as on the first pass. The capped ref is at or below the ceiling, so the
second pass through 3a clears.
