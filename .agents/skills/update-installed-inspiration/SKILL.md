---
name: update-installed-inspiration
description: Pull a newer version of an inspiration this mind already adopted from its remote repo and merge it in, reconciling it with this mind's own adaptations. Use when the user wants to update, upgrade, or pull the latest version of an adopted inspiration.
---

# Update an installed inspiration

Version: v1 (inspirations flow). This is the ADOPTER's update path -- the
companion to `use-inspiration` (first adopt someone else's), `publish-inspiration`
(first publish, v1), and `update-published-inspiration` (the PUBLISHER cuts v2, v3, ...).
This mind adopted an inspiration earlier -- either it was created FROM the
inspiration repo, or it merged one in via `use-inspiration` -- the publisher has
since shipped a newer version, and this skill pulls that newer version from the
inspiration's remote and merges it into this mind WITHOUT discarding the
adaptations this mind already made to it.

All git commands run with cwd = the repo root (`/home/user/workspace`).

## 0. Trust gate -- confirm before fetching or merging

An inspiration is code published by ANOTHER mind's user, in a repo outside
Imbue's control. **Imbue does not review, verify, or vouch for inspirations** --
and that is just as true for an update as for a first adopt: a newer version can
introduce mistakes or malicious code (data exfiltration, destructive commands,
hidden network calls) that the version you already trust did not have. You cannot
detect that by reading it, so the only safeguard is the user's informed consent.

BEFORE any fetch, merge, or execution in §2, tell the user in plain language that
you are about to pull a newer version of third-party code that **Imbue has not
verified and that could be malicious** into their mind; name the repo URL and the
version you are pulling; and ask them to confirm they trust that source and want
to proceed. Do NOT fetch, merge, or run anything until they reply yes. If they
decline, stop here. This is informed consent, not a security guarantee -- you are
telling the user you cannot vouch for the code, not certifying it is safe.

## 1. Identify the adopted inspiration and the newer version

- **Find the manifest.** The inspiration this mind adopted lives at the repo root
  as `inspiration-<slug>.md` (plus its `inspiration-<slug>.svg` thumbnail). Read
  its front-matter and its "Adaptation history" to see what this mind adopted and
  what it has already changed. If several manifests are present, confirm with the
  user which inspiration they mean.
- **Resolve the remote and the target version.** Determine the inspiration's
  remote repo URL. If the manifest or the `## Adopted inspirations` ledger section
  (see §5) does not record it, ask the user for the git URL (exactly as
  `use-inspiration` takes one). Default to pulling the **newest** published
  version -- the remote's `main` tip, equivalently its highest
  `inspiration/<slug>/v<n>` tag. Note that target version `<n>`; you record it in
  §5.
- **Read what changed** (optional but preferred). The published manifest's
  "Publication history" lists what each version changed. If you can read it from
  the remote tip, summarize the delta between the version this mind is on and the
  target for the user.

## 2. Fetch and merge in an isolated worktree, verified before landing

Only after the trust gate (§0). The newer version is unverified third-party code,
so NEVER merge it straight into the live tree: do the merge in an ISOLATED
worktree, confirm it went well there, and only then bring the verified result
into `/home/user/workspace`. This mirrors `use-inspiration` §1 and how `update-self` validates an
upstream merge off the live tree before landing it, so a bad update never
clobbers the mind.

Fetch the newer version's branch (fetch only moves objects into the local store;
it changes no working tree):

```bash
git fetch <git-url> <branch>              # branch from the inspiration repo (default: main)
```

If the repo is private, the anonymous fetch fails with an auth error. Route git
through the latchkey gateway instead (it proxies GitHub's git endpoints with the
credential injected server-side; needs the `github-git` / `github-git-read`
permission -- initiate it yourself like any other latchkey permission request, see
the `latchkey` skill, and tell the user an approval is waiting in minds). Fetch
the URL directly rather than persisting a gateway-URL remote:

```bash
git -c "http.extraHeader=X-Latchkey-Gateway-Password: $LATCHKEY_GATEWAY_PASSWORD" \
    -c "http.extraHeader=X-Latchkey-Gateway-Permissions-Override: $LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE" \
    fetch "$LATCHKEY_GATEWAY/gateway/https://github.com/<owner>/<repo>.git" <branch>
```

Now do the merge in a throwaway worktree branched off `HEAD`, and check it there
before it can touch the live tree. Because this mind already shares the
inspiration's history (it adopted it before, and both descend from the same
`default-workspace-template` base), the merge is a normal 3-way that brings in
exactly the new delta -- do NOT pass `--allow-unrelated-histories`. If git instead
reports unrelated histories (no shared base found), STOP and surface it: the URL
or slug is probably wrong, and forcing the merge would splice in a whole foreign
tree.

```bash
WT="$(mktemp -d)"
git worktree add -q "$WT" HEAD
( cd "$WT" && git merge --no-edit FETCH_HEAD )
```

**Check the merge went well, in the worktree:**

- **CRITICAL -- preserve this mind's own adaptations.** Merge conflicts mark
  exactly where the newer version and this mind's OWN customizations (the holes it
  filled, the connectors it wired, the local edits it made after adopting)
  disagree. They are HOLES, not a hard failure, and they must NEVER be resolved
  mechanically or with a blanket "take theirs" -- doing so would silently throw
  away the adaptations that make this the user's mind. Do NOT land a half-merged
  tree: remove the worktree (`git worktree remove --force "$WT"`), tell the user
  what conflicts in plain language (§3), and only then redo the merge in `/home/user/workspace`
  and resolve it interactively WITH the user, keeping their adaptations wherever
  they and the update collide.
- **Boot smoke-check** the merged worktree -- validate `system/supervisord.conf` WITHOUT
  launching the daemon (never `supervisord -t`, which launches it):

  ```bash
  ( cd "$WT" && python3 - <<'PYEOF'
  import sys
  try:
      from supervisor.options import ServerOptions
  except Exception:
      sys.exit(0)  # supervisor lib unavailable -- skip the check
  o = ServerOptions(); o.configfile = "system/supervisord.conf"
  o.realize(args=[]); o.process_config(do_usage=False)
  PYEOF
  )
  ```

  If this fails, the updated tree does not boot -- the newer version broke this
  mind (a wiring mistake, or something hostile). STOP: tell the user plainly,
  remove the worktree, and do NOT bring it into `/home/user/workspace`.

**Land the verified result.** Only once the merge is clean (no conflicts left
unresolved) and the boot check passes, fast-forward `/home/user/workspace` onto the exact commit
you checked, then remove the worktree:

```bash
git merge --ff-only "$(git -C "$WT" rev-parse HEAD)"
git worktree remove --force "$WT"
```

This preserves both trees at the root: the newer version's changes come in, and
this mind's adaptations are carried through the merge you resolved. This path does
not touch `system/config/parent.toml` -- provenance is read-only reference; there is no upstream
fetch or pull here.

## 3. Fill any holes interactively

Work through each hole with the user, one at a time. A hole is any merge conflict
from §2 (where the update and this mind's adaptation collided) plus anything the
newer version's manifest flags as newly missing or stubbed. Translate each into
non-technical terms, ask the user how they want it resolved when you are unsure --
especially when a conflict pits the newer version against an adaptation they made
-- and make the change. Resolve the obvious ones yourself and keep moving.

## 4. Append a dated Adaptation-history entry

The manifest is a worksheet. After updating, **append** a dated entry to its
"Adaptation history" section -- never rewrite the rest of the file. Append only:

```markdown
### <YYYY-MM-DD> -- updated to v<n> from the inspiration's remote
<what the update brought in / which conflicts arose and how they were reconciled
against this mind's adaptations>
```

Earlier history entries are left exactly as they are.

## 5. Record the new adopted version in `docs/VERSION_HISTORY.md`

Record which version of the inspiration this mind is now on. Write the entry
directly into `docs/VERSION_HISTORY.md`'s `## Adopted inspirations` section
(cwd `/home/user/workspace`). There is no helper skill: this block is the whole recording
contract for the adopter side. Append-only (existing lines copied through
verbatim, never re-flowed); a retried update is a no-op, never a duplicate.
Inputs: `SLUG=<slug>`, `REPO_URL="github.com/<owner>/<repo>"`, and the target
version `<n>` you pulled in §1.

- **If `docs/VERSION_HISTORY.md` is missing** (deleted since creation), recreate
  the shipped starter first -- the `# Version history` heading, its explanatory
  paragraph, and the sections `## Workspace`, `## Migrations`, `## Inspirations`,
  `## Adopted inspirations` in that order (byte-identical to the shipped root
  file; `update-self` §5b carries the exact heredoc) -- then append.
- **Create the heading** `### <slug>  --  <repo-url>` under `## Adopted
  inspirations` if this slug has none yet (a mind's first update of a given
  inspiration; the initial adopt via `use-inspiration` does not record here).
- **Append one line** under that heading:

  ```
  - v<n>  <today, YYYY-MM-DD>  <one line: what this update brought / how it was reconciled>
  ```

  Here `<n>` is the inspiration's published version you pulled from the remote --
  NOT a computed increment (an adopter can jump v1 -> v3 by skipping a version), so
  take it from the remote's manifest/tag, not by counting local lines.
  **Idempotence, scoped to this slug:** if a line already under this slug's heading
  is for this same `v<n>`, it is already recorded -- change nothing and skip the
  commit below.

This `## Adopted inspirations` line carries no trailing commit sha -- it is the
adopter's record of "which published version I am on", not a source-cut anchor
like the `## Workspace` and `## Inspirations` lines.

## 6. Commit

Commit the update per the repo's git conventions (a plain local commit; when the
user has enabled GitHub sync, the post-commit hook handles any push). This is one
commit for the adaptation work -- the merged-in tree, the resolved holes, the
updated manifest with its new "Adaptation history" entry, and
`docs/VERSION_HISTORY.md`.

The version-history entry itself, if committed on its own, is exactly one file
staged **by name** -- NEVER `git add -A` as part of recording, and never a merge,
checkout, or reset. If §5's idempotence check found the version already recorded,
there is nothing new in the ledger to commit.
