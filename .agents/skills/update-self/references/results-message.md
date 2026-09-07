# Composing the user-facing messages

These rules govern every user-facing message the update-self flow produces:
the results message after the apply, the Step 4 customization hold, and a
`stuck` result. Whenever the update cannot simply proceed, the message names
the blocker in plain terms and **proposes a way forward, or invites the user to
resolve it with you** -- it never dead-ends. What varies is how much mechanism
to keep: the results message drops technical detail the user cannot act on; a
`stuck` message deliberately preserves it, verbatim, in a marked block, so it
survives being pasted into a bug report.

## The results message

The `done` report is the lead's raw material -- a technical digest of
changelog entries, conflicts and resolutions, change classes, impact analysis
and validation. **Do not forward it verbatim.** It stays under
`data/.tasks/update-self/reports/` (offer to show it), and the results message
is composed from it, delivered *after* the apply. It is the one thing the user
reads about the whole pass: what changed, every decision made on their behalf,
any caveats, and the standing offer to roll back anything they don't like.

Write it for a non-technical reader skimming top to bottom, in this order:

1. **Verdict headline** (one line): "your workspace is updated", "updated, with
   one thing to know", or -- after a rollback -- "the update hit a problem, so
   I undid it; everything is safe".
2. **Held back by your app version** -- if and only if `held_back_by_ceiling`
   is `true` in `/tmp/update-self-target.json`: "there's a newer version
   available (`latest_available`), but it needs a newer Minds app than you're
   running, so I stopped at X". Do not derive this by comparing `ref` against
   `latest_available` yourself -- those also differ when the user's own
   `--override` picked an older tag, and the flag already accounts for that.
3. **What's new** -- always first after the ceiling note, and *detailed*: carry
   the worker's digest in prose a lay reader parses (what each change does,
   not file names). Some readers want the specifics; the rest skim it.
4. **Conflicts** -- "none", or what needed reconciling. When the worker kept
   local code over the release's version, do not present that as settled: say
   what was kept, what the release's version would have changed, and offer
   the alternative in the same breath ("I kept your version; if you'd rather
   match the official release exactly there, I can do that instead").
5. **Your customizations** -- anything the report classed intact-but-changed:
   what moved or changed (before/after, with the worker's evidence when the
   surface supports it) and the offer to restore the old arrangement. A
   cannot-be-kept creation never reaches this message unresolved; it stopped
   the pass at the Step 4 hold.
6. **Validation** -- did the suites pass; is any failure pre-existing or
   unrelated.
7. **Caveats** -- only if any: rebuild-only items, incomplete provisioning, a
   missing backup, a deviation the worker disclosed that could not be closed.
8. **Pre-existing issues** -- only if any, and only after verifying
   attribution (worker guide §4a): whether each lives in **built-in** code
   (present at the target ref -> report upstream) or the **user's own** code.
   Never call built-in code "workspace-added".
9. **The offer** -- see the language rules.

When the report marks a surface's merge work nontrivial (the system interface,
a user app), name that surface, say what was reconciled, and attach the
rollback offer to exactly that piece: the live workspace is the review surface,
since the unattended pass stands up no previews.

## Language rules

Include only what the user is likely to care about or have an opinion on. A
deferred item with no consequence they can see (a rebuild-only flag that
changes nothing until the workspace is someday recreated) is one line at most,
or nothing; a change to something they built, or a decision they might have
made differently, always makes the cut.

Detail in the informational sections (3-6); plain language at the decision
points -- the headline, any caveat that needs the user's action, and the
closing offer. Those carry no jargon: never "merge", "land" or "fast-forward"
there. Frame the close around *what changed in their workspace and how to undo
it*: "Your workspace is updated -- if anything looks or behaves differently
than you'd like, tell me and I can put it back."

Drop dependency and lockfile mechanics unless the user must act. Never print a
command *you* will run; describe it ("I'll refresh it automatically if it comes
up"). Show a literal command only when the user must run it themselves.

## The `stuck` message

Two parts: a plain-language lead for the user (what happened, what it means --
"I couldn't complete this update cleanly; your workspace is untouched and
nothing was applied" -- and a next step or an offer to work through it
together), then a clearly-marked technical block for whoever they escalate to:
the target ref, the step or phase that failed, the specific file or component,
the actual error text or log excerpt verbatim, and a pointer to the full
report and logs under `data/.tasks/update-self/reports/`. Never leave the user
at a dead end, and never hand them a failure too vague to be useful in a bug
report.

## The customization hold

From the user's point of view: what they built, what the update does to it,
the worker's before/after evidence, and the concrete choices -- apply the
update and lose it, skip the update, or the worker's best adaptation -- with a
recommendation. Reassure that nothing has been applied and the workspace is
untouched.
