# The version ceiling

The one home of the ceiling's rules. SKILL.md Steps 2 and 3a run
`resolve-target` and send you here when it refuses, or reports
`"exceeds_ceiling": true`.

The default update target **is** the release the **Imbue Studio app driving
this workspace** was built against, which `resolve-target` reads from the app
itself (`GET /api/v1/app/version` through the latchkey gateway; `ceiling` in
the output), or from the message that started the pass when the app pinned it
there (`handoff-contract.md`, "The scheduled entry"). The template carries the
code that app talks to, so only that pairing was ever verified.

Releases above the app's are treated as if they do not exist: never name one
the user did not ask for, or suggest updating the app to reach it. The Imbue
Studio app announces its own updates, on the user's release channel.

## Contents

- Reading `resolve-target`
- When no target resolves
- At the ceiling vs behind it
- Overrides past the ceiling
- When Step 3a finds the target above the ceiling

## Reading `resolve-target`

`--local-tags` reads the tags the fetch just landed. The `|| exit 1` after it
leaves a refusal's `error:` line as the last thing printed. The output
carries `ref`, `kind`, `ceiling` and `exceeds_ceiling`; an upstream branch
name such as `main` resolves to `upstream/main`. Only a version the user
names becomes an `--override` (`PINNED_OVERRIDE` in Step 2), as an operator
testing a dev build would name one.

To preview what the release changes, diff from the merge base (`git diff
--name-status "$(git merge-base HEAD "$REF")" "$REF"`), never from `HEAD`.
When the two share no commit yet, preview after Step 3a's bridge.

## When no target resolves

A non-zero exit prints one `error:` line saying why no target could be
chosen. Stop: relay it in plain terms, offer the next step, and record the
verdict it calls for. Never resolve a ref by hand, and never pick another
release.

A pass that stops here has staged, launched, bridged and paused nothing, so
there is no teardown to run: after the verdict, release the `updating
workspace` lease (`tk close "$UPDATE_LEASE_ID" "Update pass finished."`) and
end your turn with what you told the user. The same holds for a stop in Step
2a, and for a Step 3a stop before the bridge.

- **Already on the release it may take.** Nothing is wrong with the
  workspace: `run-status verdict ALREADY_CURRENT`.
- **A fault.** Three things leave the app's release out of reach, and what to
  do next is your call with the user, never a version the script picks.
  Record `run-status verdict STUCK --detail "<the error line, in plain
  terms>"` and say so plainly.
  - The app cannot be reached, or is too old to report a version. Only a pass
    with no pin asks it; a pinned pass takes the ceiling from its message, so
    an app it cannot reach is never a fault for it.
  - It names a release the upstream does not carry, so that release was never
    published as the app claims.
  - It names no release at all; a dev build reports its branch. The operator
    running one knows which ref they want: take it from them as an
    `--override`.
- **Anything else**: `run-status verdict REFUSED --detail "<the error line,
  in plain terms>"`.

## At the ceiling vs behind it

A workspace already *at* the app's release is stopped rather than passed:
there is nothing to merge, so `resolve-target` says so instead of spending a
worker and a validation run on a no-op, and the pass ends `ALREADY_CURRENT`,
not `REFUSED`. One *behind* it updates to it. The two are told apart by
whether the resolved ref is already an ancestor of `HEAD`.

## Overrides past the ceiling

`"exceeds_ceiling": true` means the user's `--override` names a version this
app cannot vouch for: newer than the app, or a branch or commit whose version
cannot be compared. Do not dispatch the worker on it silently. Tell the user
plainly what they asked for and what it risks ("that version is newer than
your Imbue Studio app, so parts of your workspace may stop working until you
update the app itself") and get an explicit go-ahead. While you wait, record
the wait with `run-status hold`, so the app shows what the pass is waiting
on, and clear it once they answer with `run-status resume`, which takes no
arguments. SKILL.md Step 2 has the `hold` command; `resume` runs the same
script.

This is the one confirmation the otherwise-unattended pass keeps: it fires at
launch, while the user is present, and asks whether to *attempt* an
unsupported version at all, a question no later rollback offer can substitute
for. An override at or below the ceiling needs no confirmation.

The message that started the pass may carry that confirmation already: a
launch that says the version is the user's explicit override, chosen
knowingly and not to be re-confirmed (the Imbue Studio app's "Update to a
specific version" prompt says so), is the same question answered up front.
Treat it as the go-ahead and do not put it to them a second time in chat; the
rollback offer after the apply stands as usual.

If the user declines, record `run-status verdict REFUSED --detail "<the
version they asked for, and that they chose not to attempt it>"` and end the
pass as "When no target resolves" ends one.

## When Step 3a finds the target above the ceiling

Step 3a resolves nothing: it either clears the target Step 2 chose or hands
the pass back to Step 2a with the ceiling's answer. Step 3a must stay in
every version of this skill (`handoff-contract.md`); `older-workspaces.md`
has the workspaces it protects. If its output has `"exceeds_ceiling": true`,
it matters who chose `$REF`:

- **Step 2 chose it, without `--override`.** The user never asked for it. Do
  not name it. Set `$REF` to the capped ref: re-run the Step 3a command
  without `--override "$REF"` to learn it, keeping the pin, and handle an
  error there as "When no target resolves" does. Tell the user that version
  is the one you are updating to.
- **The user named it.** Take the confirmation now as "Overrides past the
  ceiling" does, offering the capped ref, and record the wait with Step 2's
  `run-status hold` and `resume`, run from the staged copy's
  `update_self.py`. If they take the capped ref, set `$REF` to it. If they
  decline every option, record `run-status verdict REFUSED --detail "..."`.

Whenever `$REF` becomes the capped ref, **re-run Step 2a for it before
dispatching**, then Step 3a again. Step 2a staged the skill at the *old*
`$REF`, and the staged copy supplies the worker guide, the `update_self.py`
both agents run, and the prose you are reading: leaving it in place would run
the too-new release's copy against a target that is not it. `bootstrap-skill`
re-stages destructively, so re-running it is safe, and Step 2a's `differs`
branch then decides which document to follow, as on the first time through.
The capped ref is at or below the ceiling, so the second time through Step 3a
clears.
