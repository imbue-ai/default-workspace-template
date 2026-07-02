---
name: publish-inspiration
description: Publish a clean, shareable snapshot of the apps/features this mind built to a new GitHub repo (an "inspiration" another mind can adapt). Use when the user asks to publish, share, or export what they built as a reusable template.
---

# Publish an inspiration

An "inspiration" is a clean, shareable snapshot of the apps and features this
mind built, published to a new GitHub repo so another mind can adapt it. One
repo can accumulate several inspirations (one manifest + thumbnail per
inspiration, all at the repo root). This skill assembles the snapshot on a
clean template base, shows the user a confirmation popup, and (on confirm)
creates the repo and pushes.

All commands run with **cwd = repo root** (`/code` inside a running mind). You
own the popup, the GitHub login, and the push; the assembly + smoke-check run in
an isolated local `git worktree` in this same container (no sub-agent -- it is a
fast, deterministic script, not work that warrants delegation).

## Shared conventions

- **`$SI_BASE`** -- base URL of the in-container system_interface. Use
  `http://127.0.0.1:$SYSTEM_INTERFACE_PORT`. If `SYSTEM_INTERFACE_PORT` is
  unset, read the port from the running supervisord service definition (the
  same source `forward_port.py` registers) rather than guessing silently. All
  `/api/inspiration/*` and `/api/github-auth/*` routes are loopback-only, so
  always call them from inside the container at `127.0.0.1` (the mind always
  is).
- **Response-file poll path (absolute, fixed regardless of port):**
  `/code/runtime/inspiration/publish-response.json`. The server and this skill
  both agree on exactly this path; it is not `cwd`-relative.
- **Slug derivation.** `slug` = the user's title lowercased, with each run of
  characters outside `[A-Za-z0-9._-]` collapsed to a single `-`, and leading /
  trailing `-` stripped. The result MUST match `^[A-Za-z0-9._-]+$` and MUST NOT
  start with `-` (the backend re-validates). `repo_name` defaults to `slug`;
  the popup may override it (the backend re-validates the override). The same
  slug names the manifest (`inspiration-<slug>.md`), the thumbnail
  (`inspiration-<slug>.svg`), and the assembly branch (`mngr/<slug>`).
- **`BASE_REF` (provenance + clean base).** The FCT commit this mind was
  created from. Resolve it **in-repo, with no network access** (see step 2); do
  NOT `git fetch`/`git pull` upstream. Pass it to `build_inspiration.sh` as
  `--base-ref`.

## 1. Setup Q&A (live in chat)

Ask the user, in plain language, three things. Never enumerate files at them:

- a name for the inspiration (this becomes the title, and the slug is derived
  from it);
- which apps or features they want to include (you translate this into a set of
  repo-root-relative include paths, e.g. `apps/slack-inbox`, `libs/slack_inbox`,
  plus their service wiring -- you reason about the backing paths, the user does
  not);
- whether any data should be included. **Default: NO user data.** Include data
  paths only if the user explicitly asks for them.

Derive `slug` and `repo_name` from the title. Resolve the concrete set of
include paths yourself.

## 2. Resolve `BASE_REF` (in-repo, no network)

`BASE_REF` is the FCT base commit this mind was created from -- the initial
template commit, or the most recent `update-self:` merge marker in this mind's
own history (the same `update-self:` subject convention `update-self` / `assist`
rely on). Compute it locally, for example by finding the most recent commit
whose subject starts with `update-self:` and falling back to the root commit if
there is none. Do NOT fetch or pull from upstream to obtain it -- `parent.toml`
is a provenance link only.

## 3. Assemble on a local git worktree (same container, no sub-agent)

Assembly is a fast, deterministic script (`build_inspiration.sh`, ~a second), so
run it yourself on a throwaway `git worktree` in THIS container. Do NOT delegate
it to a `launch-task` sub-agent: that spins up a second agent with its own
boot/read/report/poll loop -- minutes of latency for a sub-second job -- and adds
no isolation, because a `git worktree` already gives a clean, separate working
tree while leaving the live mind (`/code`) completely untouched. Open one step:

```bash
tk create --step "Assemble the shareable inspiration snapshot"
# -> Created cod-step-XXXX: ...
tk start cod-step-XXXX
```

Use a **deterministic** worktree path (so later steps can find it) and a fresh
`mngr/<slug>` branch, then run the assembly script inside it. The script `cd`s to
its own worktree root, so its `git read-tree -u --reset` + `git clean -fdxq`
rewrite ONLY that worktree, never `/code`:

```bash
WT="/tmp/inspiration-<slug>"
git worktree remove --force "$WT" 2>/dev/null || rm -rf "$WT"   # clear any stale worktree
git branch -D "mngr/<slug>" 2>/dev/null || true
git worktree add -q -b "mngr/<slug>" "$WT" HEAD
( cd "$WT" && bash /code/.agents/skills/publish-inspiration/scripts/build_inspiration.sh \
    --base-ref <BASE_REF> \
    --slug <slug> \
    --title "<title>" \
    --description "<description>" \
    --include <path> [--include <path> ...] \
    [--data-include <path> ...] )
```

Check the subshell's exit code and handle the guard rails directly (§5): a
non-zero exit means nothing was committed -- surface the reason to the user and
stop. On success the assembled commit is on `mngr/<slug>`; proceed to §6 to merge
it. You own the popup, GitHub login, and push (§§6-9).

## 4. What the assembly does

`build_inspiration.sh` (documented below) does the whole assembly in the
worktree: clean base + overlay + secret scan + manifest + thumbnail + `/welcome`
rewrite + boot smoke-check + a single commit. It communicates purely via its
exit code -- `0` on success (the assembled commit is on `mngr/<slug>`), non-zero
otherwise (see §5). It prints a summary of what it assembled to stderr.

## 5. Guard rails (the script's non-zero exits)

- **Secret scan (exit 1).** A credential/token rode in on an overlaid path.
  Nothing was committed; surface the flagged path (value redacted) and stop.
- **No-diff guard (exit 3).** The resolved include set contributes nothing
  beyond `BASE_REF` (the assembled tree equals the base tree). Tell the user
  plainly and do NOT create a repo -- there are no empty inspiration repos.
- **Boot smoke-check (exit 4).** The clean base does not boot at all; abort
  BEFORE any repo creation. Selected apps having holes is expected and does NOT
  fail the check.

## 6. Merge the assembly branch and clean up

Merge the assembled commit (manifest, thumbnail, rewritten `/welcome`) into your
current working branch so it is present for the push, then remove the throwaway
worktree:

```bash
git merge --no-ff mngr/<slug>
git worktree remove --force "/tmp/inspiration-<slug>"
```

Resolve any conflicts manually, then close the assembly step.

## 7. Raise the publish popup

Build the request from the assembled values and POST it:

```bash
curl -sS -X POST "$SI_BASE/api/inspiration/publish-request" \
    -H 'Content-Type: application/json' \
    -d @- <<JSON
{
  "slug": "<slug>",
  "title": "<title>",
  "description": "<description>",
  "repo_name": "<slug>",
  "visibility": "private",
  "thumbnail_svg": <the JSON-encoded contents of inspiration-<slug>.svg>
}
JSON
```

Then poll `/code/runtime/inspiration/publish-response.json` until it exists
(check, then sleep ~5s, bounded). Read the `InspirationPublishResponse`:

- `status == "aborted"` -> stop. Leave the assembled commit intact and tell the
  user publishing was cancelled.
- `status == "confirmed"` -> use the RETURNED `title` / `description` /
  `repo_name` / `visibility` / `thumbnail_svg` for everything downstream. The
  user may have edited them, so the skill MUST use the response fields, not the
  values it originally proposed. The backend already stripped `<script>` / `on*`
  handlers / `<foreignObject>` from `thumbnail_svg`; write that sanitized value
  into `inspiration-<slug>.svg`, and re-commit the manifest/thumbnail if the
  confirmed title/description/thumbnail differ from what the assembly generated.

## 8. Ensure GitHub auth (no agent restart)

Check whether `gh` is authenticated. Run it with the token env vars scrubbed:
your agent shell inherits `GH_TOKEN`, and `gh` prioritizes that over its stored
credential, so an unscrubbed probe can report a stale/invalid env token as
"logged in" (and the later push would use that stale token instead of the
credential the modal just stored):

```bash
env -u GH_TOKEN -u GITHUB_TOKEN gh auth status --hostname github.com
```

On a non-zero exit (not logged in):

- trigger the login modal:
  ```bash
  curl -sS -X POST "$SI_BASE/api/github-auth/require"
  ```
  (the backend broadcasts `github_auth_required`; the frontend opens the
  GitHub-login modal);
- poll `GET $SI_BASE/api/github-auth/status` until `logged_in` is `true`
  (bounded wait). If the user never logs in, surface a clear message and stop,
  leaving the assembled commit intact.

The backend wires the git credential helper in place (`gh auth login` followed
by `gh auth setup-git`), so your subsequent `gh` / `git push` picks it up at
push time. Do NOT restart the agent or re-source the environment.

## 9. Create the repo and push

With `repo_name` / `visibility` taken from the confirmed response:

```bash
env -u GH_TOKEN -u GITHUB_TOKEN gh repo create "<repo_name>" --<visibility> --source=. --remote=inspiration --push
```

(`--private` or `--public` per `visibility`. `repo_name` is validated
`^[A-Za-z0-9._-]+$` server-side, which blocks argument injection, but still pass
it as a single argv element -- never interpolate it into a shell string. The
`env -u GH_TOKEN -u GITHUB_TOKEN` prefix is load-bearing: it forces `gh`/`git` to
use the credential the login modal just stored via `setup-git`, not a stale
`GH_TOKEN` inherited by your agent shell.)

**Failure handling.** If `gh repo create` fails (e.g. the name is taken or the
token lacks scope), report it to the user and re-open the publish popup (step 7)
for a new name / visibility, keeping the assembled commit intact. Loop until it
succeeds or the user aborts.

## 10. Accumulation

Publishing a mind that already holds `inspiration-*.md` manifests plus their app
dirs carries ALL of them forward into the new repo alongside the newly-published
one -- they are part of the assembled tree. The `/welcome` rewrite targets only
the newly-published slug (the latest).

## 11. Close out

Close the assembly step with a work-summary line. Report the new repo URL in
your final assistant message to the user (not in the step summary).

## The assembly script: `scripts/build_inspiration.sh`

You run `scripts/build_inspiration.sh` inside the assembly worktree (§3). It is
self-contained (the dev `create-new-mind-repo` recipe is NOT available in the
VM). Interface (cwd = worktree repo root):

```
.agents/skills/publish-inspiration/scripts/build_inspiration.sh \
  --base-ref <BASE_REF> \          # FCT commit the mind was based on (provenance + clean base)
  --slug <slug> \
  --title <title> \
  --include <path> [--include <path> ...] \   # repo-root-relative app/feature paths to overlay
  [--data-include <path> ...] \    # only when the user opted in; default none
  [--description <text>]
```

What it does, in order (see the script for the exact commands):

1. Stages the selected paths out of the current live-mind worktree into a
   scratch dir (preserving relative paths) BEFORE resetting.
2. Resets the worktree to the clean base with
   `git read-tree -u --reset <BASE_REF>` then `git clean -fdxq` -- this drops
   tracked-but-not-in-base files AND gitignored cruft (secrets, runtime state).
   It never `git checkout <ref> -- .` (that leaks the mind's whole committed
   tree) and never fetches/pulls upstream.
3. Overlays the staged paths onto the clean base with
   `rsync -a "$STAGE/" "$REPO/"` (root-to-root contents merge) -- never a
   nesting copy like `cp -a "$STAGE/apps" "$REPO/apps"`.
4. Carries forward any existing accumulated `inspiration-*.md` + `.svg` at the
   repo root.
5. Runs a deterministic secret scan that HARD-FAILS (non-zero, abort before any
   commit/push) on token patterns and credential filenames. This is the
   authoritative blocker, not LLM prose.
6. Generates the manifest `inspiration-<slug>.md` at the repo root.
7. Generates a placeholder thumbnail `inspiration-<slug>.svg` (mock data only;
   the lead may later overwrite it with the popup-confirmed sanitized SVG).
8. Rewrites only the marked stable region of `welcome/SKILL.md` to describe the
   newly-published inspiration.
9. Validates `supervisord.conf` WITHOUT starting the daemon (never
   `supervisord -t`), then makes a single commit for the assembled snapshot.
