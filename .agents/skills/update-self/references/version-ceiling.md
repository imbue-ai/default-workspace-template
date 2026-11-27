# The version ceiling

The default update target **is** the release this workspace's **release
channel** is on (`ceiling` in the output). The workspace holds the channel in
its update settings and reads the channel's version off the release feed
itself, at most once an hour (`data/.state/update-apply/settings.json` and
`channel-version.json`, kept by the update dispatcher), and `resolve-target`
reads what it last read. No Imbue Studio app is asked, so a workspace whose
owner has no desktop app running resolves like any other. A pass the workspace
or Imbue Studio started names the ceiling in its first message instead
(`handoff-contract.md`, "The scheduled entry").

One workspace still asks an app: one that holds no update settings at all,
because the only Imbue Studio that has opened it is a desktop app from before
those settings existed. `resolve-target` then takes the release that app says
it supports, as every pass did before, and only when no app answers is the
missing channel a fault. The fault then says why the app did not answer, which
is what the user can act on: an app too old to report its version is updated
first, and one that is closed is started.

Three things leave that release out of reach, and all three are **faults**, not
refusals: `resolve-target` fails, and what to do next is your call with the
user, never a version the script picks.

- The workspace has read no version for its channel: it holds no update
  settings yet (Imbue Studio writes them when it opens the workspace) and no
  desktop app answered for it, or the channel's file could not be read. Only a
  pass with no pin depends on it; a pinned pass takes the ceiling from its
  message.
- The ceiling names a release the upstream does not carry -- so that release
  was never published as claimed.
- It names no release at all; a development build of Imbue Studio pins its
  branch. The operator running one knows which ref they want: take it from
  them as an `--override`.

Releases above the ceiling are treated as if they do not exist: never name one
the user did not ask for. The workspace's channel reaches a release when it is
promoted there, and the workspace then updates to it.

## At the ceiling vs behind it

A workspace already *at* the ceiling's release is stopped rather than passed: there
is nothing to merge, so `resolve-target` says so instead of spending a worker
and a validation run on a no-op, and the pass ends `ALREADY_CURRENT`, not
`REFUSED`. One *behind* it updates to it. The two are
told apart by whether the resolved ref is already an ancestor of `HEAD`.

## Overrides past the ceiling

`"exceeds_ceiling": true` means the user's `--override` names a version
nothing vouches for -- newer than the ceiling, or a branch or commit whose
version cannot be compared. Do not dispatch the worker on it silently. Tell the
user plainly what they asked for and what it risks ("that version is newer than
the one your workspace is set to follow, so parts of it may not work with your
Imbue Studio app yet") and get an explicit go-ahead. This is the one confirmation the
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
arriving from an older template. A workspace arriving from a template from
before the update settings has read no channel's version, so an unpinned 3a
there has no ceiling to hold `$REF` to when no desktop app answers either, and
clears it.

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
