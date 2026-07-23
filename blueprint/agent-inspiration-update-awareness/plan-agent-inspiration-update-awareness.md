# Plan: agent inspiration update awareness (status, drift, and updating a published inspiration)

> **Give every published inspiration a durable back-link to the exact source state it was cut from, so an agent can tell whether it is stale and what changed since -- and add an `update-inspiration` flow that re-assembles the delta and advances the published repo by exactly one clean commit (v2, v3, ...), without ever loosening publish-inspiration's bootable / base-history / no-pre-cleanup-leak invariants.**
>
> ### The gap
> * A publish writes nothing back to `/code` (the CWD invariant makes the live checkout "done being touched" after assembly) and mints the snapshot commit parented on `BASE_REF` (the shared template base), never on the source workspace's HEAD (an explicit privacy invariant). So today the only trace a publish leaves is the remote repo existing on GitHub -- there is no record, anywhere the source-side agent can read, of *which slug was published, from what source commit, to which repo, at what version*. An agent cannot answer "is my inspiration stale?" and an adopter cannot answer "has it drifted?"
>
> ### Recommended primary mechanism
> * **A committed provenance ledger in the source workspace** (an extension of the version-history `.md` the parallel `update-self` change is adding) is the record of record: per published slug it holds repo URL/owner/name, the **source HEAD sha at publish**, `BASE_REF`, the **published snapshot sha**, inspiration **version n**, date, visibility, and the include set. Chosen over tags-as-primary because it is durable (committed -> carried by `github-sync`, unlike a bare ref) and reuses the codebase's existing pattern of keying provenance off committed markers, not tags.
> * **Complemented by an annotated git tag `inspiration/<slug>/v<n>`** pushed into the **published repo** at each snapshot commit -- the durable, adopter-facing version marker and the recovery index for the version counter if the ledger is lost. A same-named lightweight tag in the source is an optional convenience for `git diff`, not the record of record (source refs are not reliably synced).
>
> ### Updating a published inspiration
> * `update-inspiration <slug>` reads the ledger, fetches the published repo's current `main`, verifies its tip matches the recorded snapshot sha, and computes the delta (`git diff <recorded-source-sha> HEAD` over the recorded include paths). No delta -> tell the user it is already current.
> * The update **re-assembles from the published tip's tree** (not from raw `BASE_REF`), so the finished manifest prose, Prerequisites/Holes, bespoke thumbnail, and adopters' Adaptation history are preserved; only the newer app changes are overlaid, re-scanned for secrets, and the same published-version modifications re-applied.
> * It mints **one clean commit parented on the previous published tip** (fast-forward push, no force), appends a **Publication history** entry (distinct from adopters' Adaptation history), bumps to **v(n+1)**, moves the tag, and updates the ledger. The base-history + all-commits-above-base-are-post-cleanup invariant holds; `merge-base(template, tip)` stays `BASE_REF`, so composability is unchanged.
>
> ### Scope note
> * This proposal designs behavior only. It does not weaken any publish-inspiration guard-rail; it adds a deliberate, post-push, single-file ledger commit to `/code` (the one sanctioned exception to "done being touched") and a new update lead beside publish/use.

## Overview

- An "inspiration" is a bootable snapshot of what a mind built, published to a fresh GitHub repo that another mind can be created from or adapt (`publish-inspiration`, `use-inspiration`). The publish flow is deliberately one-directional and privacy-preserving: an isolated worker assembles a clean tree on top of `BASE_REF`, the lead confirms with the user and pushes a single snapshot commit, and `/code` is never touched after assembly (the CWD invariant). That one-directionality is exactly what leaves no provenance behind.
- The motivating capability: an agent (in the source mind) should be able to say "the `slack-inbox` inspiration you published is 14 commits behind your current app -- want me to update it?" and then actually do the update; and an adopter should be able to learn its source inspiration moved and see what changed. Neither is possible today because nothing links the published snapshot back to the exact source state.
- The structuring principle mirrors the existing split of readers: the **source-side agent** (answering "is what I published stale, and what changed since?") and the **adopter-side agent** (answering "has what I adopted drifted or been updated?") need different records. The source-side record must live in the source workspace and be durable; the adopter-facing record must live in the published repo. The design gives each its own home rather than overloading one.
- The design is strictly additive to `publish-inspiration`: every existing invariant (bootable-or-nothing, two-commit / base-history, no-pre-cleanup-leak, private-by-default, hard secret scan, both chat gates) is preserved verbatim. The new material is (1) provenance recording appended after a successful publish, (2) a status/awareness read path, and (3) a new `update-inspiration` lead that reuses the same worker + `build_inspiration.sh` machinery with an "update" starting tree.

## The problem

- **Publishing leaves no source-side trace.** Grounded in the skills as they stand:
  - `build_inspiration.sh` step 10 mints the snapshot with `git commit-tree "$(git write-tree)" -p "$BASE_REF"` and a message that records only the slug and `BASE_REF`. The comment is explicit that parenting on the mind's HEAD is forbidden ("Parenting on HEAD would ship every commit the mind ever made ... defeats published-version modifications entirely"). So the published commit carries the **template base sha**, which is shared across many minds, and *not* the source workspace's own HEAD.
  - `publish-inspiration` SKILL §8's push sends `<snapshot-sha>:refs/heads/main`; the flow writes **no git tag, no git note, no version field** anywhere (the only `version` present is `INSPIRATION_FLOW_VERSION = v1`, the *flow/manifest-format* version, in the manifest front-matter `format:` key and the repo-description suffix `(minds inspiration v1)` -- not a per-inspiration version).
  - The CWD invariant (SKILL §3 callout) makes `/code` "DONE being touched" from the moment assembly finishes; the flow never records into `/code` that a publish happened.
  - Net effect: the source workspace has **zero committed record** of what it published, from which commit, to which repo. The manifest front-matter generated by `build_inspiration.sh` (step 6) has `title`, `description`, `thumbnail`, `format` -- no source sha, no version, no repo URL.
- **Consequence for the source-side agent:** it cannot compute a delta since publication because it does not know the "since" point. There is no anchor commit to `git diff` against, so "is this inspiration stale?" is unanswerable. Meanwhile the source keeps evolving -- new app commits, `update-self` moving `BASE_REF` forward, published-version modifications diverging from live files -- and none of it is measurable against the published snapshot.
- **Consequence for the adopter:** an adopted mind (or a mind merged an inspiration in via `use-inspiration`) records what it did in the manifest's Adaptation history, but has no cheap way to learn the *upstream* published repo advanced, nor what changed, because there is no version marker on the published side to compare against and no published-side changelog of updates.
- **Two distinct axes of "staleness"** that the current model cannot even name, let alone report:
  - *app-delta*: the included app/feature paths changed in the source since publish.
  - *base-delta*: `BASE_REF` moved (the mind ran `update-self`), so the template substrate the snapshot sits on is older than the mind's current base -- even if the app itself is unchanged.
  These are different questions with different remedies, and any status mechanism must distinguish them.

## The status / awareness mechanism

The mechanism must let a source-side agent answer, per published slug: *what did I publish, from what source commit, to which repo, at what version, and what has changed since* -- durably (surviving container loss) and cheaply (a local read plus a `git diff`).

### Option A -- git tags (evaluated concretely)

- **Published-repo tag** `inspiration/<slug>/v<n>` at the snapshot commit, pushed alongside the branch. Strong as a *published-side* version marker: it is durable (lives on GitHub), it is the natural thing an adopter compares against, and `git tag --list 'inspiration/<slug>/v*'` recovers the version counter if all else is lost. This is worth adopting.
- **Source-workspace tag** at the source HEAD the publish was cut from, annotated with the repo URL + snapshot sha. Attractive because `git diff inspiration/<slug>/v<n> HEAD -- <paths>` is then a one-liner for the app-delta. **But** source refs are not reliably durable: `github-sync`'s post-commit hook pushes the *active branch*, and there is no guarantee tags ride along; on container loss an un-synced tag is gone. Tags also carry only an opaque pointer -- no structured record of repo URL, visibility, include set, or version history -- so a tag alone cannot drive the update flow or a human-readable status.
- Verdict: tags are an excellent *index/pointer*, a poor *record of record* on the source side.

### Option B -- a metadata block in the published manifest

- Record source sha + `BASE_REF` + snapshot sha + version in the `inspiration-<slug>.md` front-matter (or a Publication-history section). Durable (on GitHub) and exactly where an adopter looks. Adopt this for the **published-facing** version/changelog -- but keep the **source workspace's internal commit sha out of the published repo** (privacy: an opaque id that only resolves inside the private source is provenance, not content, but there is no reason to ship it; the published side needs only snapshot sha + version + date + "what changed" prose).
- Insufficient as the source-side record: it lives in the remote, not where the source agent reads, and it cannot hold the source anchor sha.

### Option C -- the committed source-workspace ledger (recommended primary)

- **Extend the workspace version-history `.md` that the parallel `update-self` change is adding** (assumed to land) into the authoritative provenance ledger. Per published slug, append/maintain a row:
  - repo URL, owner, repo name;
  - **source HEAD sha at publish** (the anchor for `git diff`);
  - `BASE_REF` at publish (to detect base-delta);
  - **published snapshot sha** (to verify the remote tip before an update);
  - inspiration **version n**;
  - publish date, visibility;
  - the resolved include / data-include paths, and the published-version modifications applied.
- This is committed, so `github-sync` carries it to the workspace's private sync repo -- it survives container loss, unlike a bare ref. It is human- and agent-readable in the file the user (post-parallel-change) already has for "what template version am I on." And it reuses the codebase's established pattern: `BASE_REF` itself is resolved off *committed* commit-subject markers (`update-self:` / `Initial workspace commit`), not tags -- provenance in this system is committed content, and this ledger is consistent with that.

### Option D -- the `minds-inspiration` GitHub topic

- Useful only for *discovery* of inspirations as a group (topic search). It carries no per-inspiration state and cannot express version or drift. Keep it as-is; it is orthogonal to status.

### Recommendation

- **Primary: the committed source-workspace ledger (Option C)** -- one-line reason: it is the only candidate that is both durable (committed and synced, where refs are not) and rich enough to drive both status and the update flow, and it matches how the codebase already records provenance (committed markers, not tags).
- **Complement: the published-repo annotated tag `inspiration/<slug>/v<n>` (Option A, published side) + a Publication-history section in the published manifest (Option B, published side)** -- the durable, adopter-facing version marker and changelog, and the recovery index for the version counter.
- **Deliberately excluded from the published side: the source workspace's internal commit sha** -- it stays only in the source ledger.

### How an agent computes status from it

Given the ledger row for `<slug>`:

- **app-delta:** `git diff --stat <source-sha-at-publish> HEAD -- <include paths>` (and a name-status to catch new files under those paths). Empty -> app is current; non-empty -> "N files changed since v(n)."
- **base-delta:** compare the ledger's `BASE_REF` against the current resolved base (SKILL §2's marker walk). Different -> "your template base advanced since this was published."
- **remote integrity:** fetch the published repo's `main` tip (anonymously, or via latchkey `github-git-read` for a private repo) and confirm it equals the recorded snapshot sha; a mismatch means the repo was changed out-of-band (an adopter pushed, a manual edit) and any update must reconcile before proceeding.
- Report both axes plainly; they have different remedies (re-publish the app vs. re-cut on a newer base vs. both).

## Updating a published inspiration

Flow for "update my inspiration `<slug>`". It is a new lead, `update-inspiration`, that reuses `publish-inspiration`'s worker + `build_inspiration.sh` machinery rather than duplicating it. It preserves every publish invariant.

1. **Find the existing repo (from the ledger).** Read the `<slug>` row: repo URL/owner/name, last source sha, last snapshot sha, last version n, include set, prior published-version modifications. If there is no row (published before this feature, or ledger lost), fall back to the published-repo tags for the version counter and ask the user to confirm the repo URL; reconstruct a minimal row before proceeding.
2. **Verify the remote is where we left it.** Fetch `main`; require its tip == recorded snapshot sha. On divergence, stop and surface it -- do not silently overwrite an out-of-band change.
3. **Compute the delta.** `git diff <recorded-source-sha> HEAD` over the recorded include paths (plus any new paths the user wants to add -- a scope change that re-opens the §1 scope gate). If the delta is empty and `BASE_REF` is unchanged, tell the user it is already current and stop (mirrors `build_inspiration.sh`'s exit-3 no-diff guard, applied to the update).
4. **Re-assemble from the published tip, not raw `BASE_REF`.** This is the key difference from a first publish. A naive re-run of `build_inspiration.sh` resets to `BASE_REF` and regenerates the manifest/welcome/readme/thumbnail from scratch -- which would **destroy** the finished manifest prose, Prerequisites/Holes, the bespoke thumbnail, and adopters' Adaptation history. Instead, the update worker takes the **fetched published tip's tree** as the starting tree, overlays only the newer included paths from the source, and leaves the already-finished manifest/thumbnail/welcome in place. Realize this as an explicit "update" mode of the assembly (e.g. `build_inspiration.sh --update --from-published-tip <sha>` resetting to the fetched tip instead of `BASE_REF`, keeping the same overlay + scan + boot-smoke-check steps), so the mechanical guarantees are shared code, not re-implemented.
5. **Re-apply published-version modifications and re-scan secrets.** Re-apply the prior generalizations (recorded in the ledger) plus any new ones the user asks for, then run the same `scan_secrets.sh` gate over every newly overlaid/modified path -- a secret introduced in the source since v(n) can ride in on an updated path exactly as at first publish. The scan stays the authoritative, hard-failing blocker.
6. **Manifest: append a Publication-history entry (new section, not Adaptation history).** Adaptation history is the *adopters'* log ("each mind that adapts appends one dated entry"); publisher updates must not write there. The manifest already carries a distinct **Publication history** section, seeded at v1 publish by `build_inspiration.sh` (`### v1 (date) -- what this first version publishes`); the *publisher* appends to it on each update: `### v<n+1> (YYYY-MM-DD) -- <what changed since v(n)>`. Earlier entries are never rewritten. The thumbnail is re-confirmed with the user and only changed if they want.
7. **Mint one clean commit parented on the previous published tip; fast-forward push.** `git commit-tree <final-tree> -p <published-tip-sha>` -> the published `main` advances by exactly one commit. The invariant that matters -- *every commit above the template base is a single, atomic, post-cleanup snapshot with no intermediate pre-cleanup state* -- is preserved: v(n+1) is minted atomically from the final, scanned, generalized tree. Because the published tip already descends from `BASE_REF`, `merge-base(template, v(n+1))` is still `BASE_REF`, so an adopter's 3-way merge still brings in exactly the cumulative delta. The push is a fast-forward (parented on the current tip), so **no force-push** is needed. Reuse §8's guards adapted: assert the new commit descends from the recorded snapshot sha, and that `rev-list --count > 1`.
   - **Invariant-wording caveat to resolve:** `publish-inspiration` SKILL §8 currently says "never more than one inspiration commit." Read in context, that rule targets *intermediate assembly commits within a single publish leaking pre-cleanup state* -- not a prohibition on a second clean snapshot published later. The update flow keeps the spirit (no pre-cleanup state ever exists as its own commit) while adding clean commits over time. This interpretation must be blessed and the §8 wording clarified; if instead the strict "one inspiration commit total, forever" reading is intended, the alternative is a re-mint on `BASE_REF` + force-push (which rewrites published history and loses the update trail) -- worse, and I recommend against it.
8. **Bump the tag and update the ledger.** Move/create `inspiration/<slug>/v<n+1>` in the published repo at the new snapshot commit; append a Publication-history entry client-side is already done in step 6. Then, **after the push succeeds**, append a new ledger row in the source (new source sha, new snapshot sha, v(n+1), date) -- see the CWD-invariant note under Risks; this is the one deliberate write back to `/code`.
9. **Re-confirm with the user -- both gates, as at first publish.** The scope gate (what changed, any newly included paths, modifications to re-apply) and the final chat confirmation (the change summary, the new version number, the thumbnail if touched, visibility unchanged) both re-run. An update publishes new content to the user's account, so it is not exempt from confirmation; no earlier approval carries over.

## Adopter-side awareness (optional, high value)

- **What the adopter records at adoption.** `use-inspiration` already appends an Adaptation-history entry. Extend it to also record, in a committed adopter-side note, the (repo URL, adopted snapshot sha / adopted `inspiration/<slug>/v<n>` tag) it merged. This is the adopter's "since" anchor.
- **Learning of an update.** A small capability (a note in `use-inspiration`, or a tiny `check-inspiration-updates` skill) re-fetches the published repo, compares the latest `inspiration/<slug>/v*` tag (or `main` tip sha) against the adopted anchor; if newer, it reads the manifest's **Publication history** to summarize what changed between the adopted version and the latest, and offers to pull it in. Because both trees still share `BASE_REF` as merge-base, pulling the update is the same `git merge --allow-unrelated-histories` / 3-way path `use-inspiration` already documents -- the update brings in just the new delta.
- **Private repos** need latchkey `github-git-read` to poll, initiated the same way `use-inspiration`'s merge path already does for private fetches.
- This is optional and can be a later phase; the source-side status + update flow stand on their own.

## Open questions, risks, and phasing

### Risks and security

- **CWD-invariant tension (the sharpest one).** `publish-inspiration` declares `/code` "DONE being touched" after assembly. Recording the ledger means one deliberate write back to `/code`. The invariant's actual danger is *tree-clobbering operations run from `/code`* (the merge/checkout that once reset a live tree to an old base) -- **not** a normal single-file commit. There is precedent: the parallel `update-self` change already commits a version-history `.md` into `/code`. So the design sequences the ledger update as a distinct final step **after the push succeeds**: add exactly the ledger file, commit it on the current branch, never a merge/checkout, never from the worker's worktree. This must be explicitly blessed in the skill text so it is not mistaken for the forbidden pattern. Alternative if even that is unwanted: record into a `refs/notes/inspirations` note or a tag (no tree mutation) -- but durability/sync then suffers, which is why the committed ledger is preferred.
- **Never leak pre-cleanup state.** The update re-applies the prior published-version modifications and re-scans; the single-atomic-clean-commit mint is preserved, so no intermediate (pre-generalization) tree is ever a commit. The source's own commit history still never leaves the machine (the mint parents on the published tip, which parents on `BASE_REF`, never on source HEAD).
- **Private-by-default is unchanged.** An update never changes visibility; the confirmation restates it. Polling a private published repo for adopter awareness uses latchkey, never a token in a URL.
- **Do not ship the source sha to the published side.** It stays in the source ledger only; the published manifest/tag carry the snapshot sha + version + human-readable changelog.
- **Out-of-band divergence.** If the published `main` tip no longer matches the recorded snapshot sha (an adopter or manual edit pushed), the update stops and surfaces rather than force-advancing.

### Open questions

- **Ledger location and shape.** Exactly how to fold the inspiration provenance into the `update-self` version-history `.md` (a dedicated section vs. a sibling `inspirations.md` vs. front-matter) depends on that file's final format, which is not in the tree yet -- flagged as an assumption; this proposal assumes it lands and is committed.
- **Version counter source of truth.** Ledger `n` is primary; published-repo tags are the recovery index. If they disagree (ledger lost and reconstructed), which wins, and how to detect a gap.
- **Base-delta remedy.** When only `BASE_REF` moved, is the right action to re-cut the inspiration on the newer base (a bigger operation than an app-delta update, since the whole substrate changes) or to leave it and just report? Likely user-decided; needs a defined default.
- **Proactive vs. on-demand status.** Should status surface unprompted (a manifest section, a `/welcome` hint, a periodic check) or only when the user asks? Start on-demand.
- **Multi-inspiration repos.** One repo can accumulate several inspirations (SKILL §9). Tags are per-slug (`inspiration/<slug>/v<n>`), so they compose; confirm the update flow touches only the target slug's manifest/thumbnail and leaves the others' Publication history untouched.
- **`build_inspiration.sh --update` reset target.** Confirm resetting to a fetched published tip (rather than `BASE_REF`) still satisfies the boot smoke-check and no-diff guard, and that carry-forward of accumulated `inspiration-*.md/.svg` still behaves (they are already in the published tip).

### Suggested phasing

- **Phase 1 -- record provenance (minimal, additive, low risk).** On a successful publish, (a) append the ledger row in the source (the one sanctioned post-push `/code` commit) and (b) push the `inspiration/<slug>/v1` tag into the published repo. No update flow yet. This alone makes app-delta and base-delta computable (`git diff <recorded-sha> HEAD`) -- it closes the core gap.
- **Phase 2 -- status/awareness read path.** A lightweight source-side capability that reads the ledger and reports per-slug drift (app-delta + base-delta + remote-integrity check), plainly, on demand.
- **Phase 3 -- `update-inspiration <slug>`.** The full re-assembly-from-published-tip flow: delta -> `--update` assembly -> re-scan -> Publication-history entry -> v(n+1) mint on the published tip -> fast-forward push -> tag bump -> ledger update, with both chat gates.
- **Phase 4 -- adopter-side awareness.** Record the adopted anchor in `use-inspiration`, and add the poll-and-summarize update check.

## Grounding and assumptions (flagged)

- Every "today it does X" claim is read from `.agents/skills/publish-inspiration/SKILL.md`, `.agents/skills/publish-inspiration/scripts/build_inspiration.sh`, and `.agents/skills/use-inspiration/SKILL.md`: the snapshot commit is `git commit-tree ... -p "$BASE_REF"` recording only slug + `BASE_REF` (step 10 / §8); no tag/note/version is written anywhere; `/code` is untouched after assembly (§3 CWD callout); manifest front-matter is `title/description/thumbnail/format` only (step 6); the `minds-inspiration` topic is discovery-only (§8 step 3); accumulation and Adaptation history semantics per §9 / manifest template.
- **Assumption that may be wrong:** the parallel `update-self` version-history `.md` is not in this checkout yet, so its exact format is unknown. This plan assumes it lands as a committed, synced file and treats it as the natural home for the ledger; if it does not land, Phase 1 should introduce a standalone committed `inspirations.md` ledger instead -- the mechanism is identical, only the file differs.
- **Interpretation that must be confirmed:** SKILL §8's "never more than one inspiration commit" is read here as "no *intermediate pre-cleanup* commit within a publish," not "one inspiration commit total forever." The update flow depends on that reading; if the strict reading is intended, updates cannot advance `main` cleanly and would need a force-push re-mint (not recommended).
