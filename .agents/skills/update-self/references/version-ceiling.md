# The version ceiling

The default update target is capped at the version of the **Minds app driving
this workspace**, which `resolve-target` reads from the app itself (`GET
/api/v1/app/version` through the latchkey gateway; `ceiling` in the output).
The template carries the code the app talks to -- the system interface and the
vendored `mngr` -- so a workspace running a template newer than its app would
be speaking a protocol the app does not know. When the app reports a branch
rather than a release tag (a dev build) there is nothing to compare against and
`ceiling` caps nothing. When the app cannot be reached, or is too old to report
a version, `resolve-target` **fails** rather than updating uncapped.

## At the ceiling vs behind it

A workspace already sitting *at* the ceiling gets a refusal rather than a pass:
the capped target is the release it was created from, so there is nothing to
merge, and `resolve-target` says so instead of spending a backup, a worker and
a validation run on a no-op -- naming the newer release the app is holding back
when there is one (`held_back_by_ceiling`, `latest_available`). A workspace
*behind* the ceiling still updates to it. The two are distinguished by whether
the resolved ref is already an ancestor of `HEAD`, not by the ceiling alone.

## Overrides past the ceiling

`"exceeds_ceiling": true` means the user's `--override` names a version this
app cannot vouch for -- newer than the app, or a branch or commit whose version
cannot be compared. Do not dispatch the worker on it silently. Tell the user
plainly what they asked for and what it risks ("that version is newer than your
Minds app, so parts of your workspace may stop working until you update the
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
back to 2a with the ceiling's answer. If the user takes the capped ref, `$REF`
changes, and §2a must be re-run for it before dispatching: §2a staged the
skill at the *old* `$REF`, and the staged copy supplies the worker guide, the
`update_self.py` both agents run, and the prose the lead is reading -- leaving
it in place would run the too-new release's flow against a target that is not
it. `bootstrap-skill` re-stages destructively, so re-running it is safe, and
2a's `differs` branch then decides which document to follow, as on the first
pass. The capped ref is at or below the ceiling, so the second pass through 3a
clears.
