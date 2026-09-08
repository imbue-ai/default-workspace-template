---
name: use-template
description: Adapt an existing template (a published snapshot of apps/features from another mind) into this mind, resolving its requirements interactively. Use when the user gives a template's git URL, or asks to adopt/adapt/reuse a published template.
metadata:
  author: imbue
---

# Adapting a template

Version: v2 (templates flow). This versions the publish/adopt flow and the
manifest format. This skill reads BOTH:

- **v2** -- exactly one slug-free `template.md` + `template.toml` +
  `template.svg` per repo. The TOML is authoritative for the recipe, the
  requirements, the environment to converge, and the lineage.
- **v1** -- the older format, which could accumulate several slug-named
  `inspiration-<slug>.md` (+ `.svg`) in one repo, the
  recipe as a YAML block inside the markdown, and no TOML at all. Absence of
  `template.toml` is what identifies it. Still adopted exactly as before;
  nothing writes this format any more.

A template is a publishable, reusable snapshot of the apps and features a mind
has built. It lives in its own GitHub repo as a real default-workspace-template tree
plus its manifest at the repo root -- `template.md` with a
`template.svg` thumbnail (v2 adds `template.toml`; a v1 repo instead has
slug-named `inspiration-<slug>.md`, possibly several). Adapting a template means bringing
that snapshot into *this* mind and then working through its "requirements" — the parts
the original author left stubbed or unwired — together with the user.

All git commands run with cwd = the repo root (`/home/user/workspace`).

## Two entry points

There are two ways this skill starts. Figure out which one applies before doing
anything else.

**A. Template path — this mind was created from a template repo.** The mind
already has the template's tree at its root (it *is* the template repo), so
there is nothing to fetch. On this path adaptation starts IMMEDIATELY at boot:
the published repo ships its own template-specific `/welcome` skill
(generated into the snapshot by the publish flow, replacing the template's
generic welcome), so the booting agent's first response is a custom welcome
naming the template's title and one-line description (instead of the generic
"Welcome to Minds" message), followed in the same turn — without waiting to be
asked — by reading the manifest and asking the user how they want to adapt it.
The manifest's "How to adapt it" section is the script for that conversation.
A v2 repo has exactly one `template.md`, so there is nothing to choose:
adapt it. (Only an older v1 repo can hold several slug-named
`inspiration-<slug>.md` files; there, take the latest slug named in the welcome
skill, treat the others as already-adapted reference material, and ask the user
if it is ambiguous.) Skip step 1 below (the tree is already
here) and go straight to reading the manifest.

**B. Merge path — the user gave you a template's git URL.** Bring the
template into the *current* mind at the repo root, then adapt it. Do step 1
below to merge it in.

## 0. Trust gate — confirm before merging in (merge path B only)

A template is code published by ANOTHER mind's user, in a repo outside
Imbue's control. **Imbue does not review, verify, or vouch for templates.**
Adopting one runs its code in this mind -- its services, skills, and scripts --
and it could contain mistakes or malicious code (data exfiltration, destructive
commands, hidden network calls). You cannot detect that by reading it, so the
only safeguard is the user's informed consent.

On the **merge path (B)**, BEFORE any fetch, merge, or execution in §1, tell the
user in plain language that you are about to pull third-party code that **Imbue
has not verified and that could be malicious** into their mind; name the repo
URL; and ask them to confirm they trust that source and want to proceed. Do NOT
fetch, merge, or run anything from the template until they reply yes. If they
decline, stop here. This is informed consent, not a security guarantee -- you
are telling the user you cannot vouch for the code, not certifying it is safe.

The **template path (A)** needs no such gate: creating a mind from a
template repo WAS the trust decision, so a mind already built from one is
treated as trusted -- go straight to adapting it.

## 1. Bring in the template, verified in a worktree (merge path only)

Only after the trust gate (§0). The template is unverified third-party code,
so NEVER merge it straight into the live tree: do the merge in an ISOLATED
worktree, confirm it went well there, and only then bring the verified result
into `/home/user/workspace`. This mirrors how `update-self` validates an upstream merge off the
live tree before landing it.

Do NOT use `git subtree add --prefix=.` — subtree does not support the repo root
as its prefix and errors out. First fetch the template's branch (fetch only
moves objects into the local store; it changes no working tree):

```bash
git remote add template <git-url>        # or a uniquely-named remote if 'template' is taken
git fetch template <branch>              # branch from the template repo (default: main)
```

If the repo is private, the anonymous fetch fails with an auth error. Route git
through the latchkey gateway instead (it proxies GitHub's git endpoints with the
credential injected server-side; needs the `github-git` / `github-git-read`
permission -- initiate it yourself like any other latchkey permission request,
see the `latchkey` skill). Fetch the URL directly rather than persisting a
gateway-URL remote:

```bash
git -c "http.extraHeader=X-Latchkey-Gateway-Password: $LATCHKEY_GATEWAY_PASSWORD" \
    ${LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE:+-c "http.extraHeader=X-Latchkey-Gateway-Permissions-Override: $LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE"} \
    fetch "$LATCHKEY_GATEWAY/gateway/https://github.com/<owner>/<repo>.git" <branch>
```

Now do the merge in a throwaway worktree branched off `HEAD`, and check it there
before it can touch the live tree:

```bash
WT="$(mktemp -d)"
git worktree add -q "$WT" HEAD
( cd "$WT" && git merge --allow-unrelated-histories --no-edit FETCH_HEAD )
```

**Check the merge went well, in the worktree:**

- **Merge conflicts** are HOLES, not a hard failure: they mark where the
  template and this mind's tree disagree. Do NOT resolve them mechanically or
  land a half-merged tree -- remove the worktree (`git worktree remove --force
  "$WT"`), tell the user what conflicts (step 4, plain language), and only then
  redo the merge in `/home/user/workspace` and resolve it interactively with them.
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

  If this fails, the merged tree does not boot -- the template broke this
  mind (a wiring mistake, or something hostile). STOP: tell the user plainly,
  remove the worktree, and do NOT bring it into `/home/user/workspace`.

**Land the verified result.** Only once the merge is clean and the boot check
passes, fast-forward `/home/user/workspace` onto the exact commit you checked, then remove the
worktree:

```bash
git merge --ff-only "$(git -C "$WT" rev-parse HEAD)"
git worktree remove --force "$WT"
```

This preserves both trees at the root. The template's `template.md`
manifest(s) and their `.svg` thumbnails land at the repo root alongside anything
this mind already had.

This merge path does not touch `system/config/parent.toml` — provenance is read-only reference
(the template records only a link to the default-workspace-template base it was
built from; there is no upstream fetch or pull here).

## 2. Read the manifest

The manifest lives at the repo root. A **v2** template has exactly one, in
three files: `template.md` (prose), `template.toml` (the machine-readable
half), and `template.svg` (the thumbnail). Read the TOML first -- its
presence is what tells you the format:

- **`template.toml` present (v2).** It is authoritative for the identity,
  the `[recipe]`, the `[requirements]`, the `[environment]` this template
  needs installed, and the `[[lineage]]` of templates it was built on. Read
  `template.md` alongside it for the prose: `What it is`, `How it works`,
  `Requirements`, `Environment`, `How to adapt it`, and `Adaptation history`.
- **No `template.toml` (v1).** An older template: one or more slug-named
  `inspiration-<slug>.md` files and no TOML. Read the markdown exactly as
  before -- front matter (`title`, `description`, `thumbnail`, and optionally
  `format`), then the body sections. If several are present, take the latest
  slug named in the repo's `/welcome` skill, or ask the user which they mean.
  Older manifests may have `Apps included` instead of `How it works`,
  `Permissions it may need` instead of `Prerequisites`, `Holes` instead of
  `Requirements`, and no `How to adapt it`. A v1 template declares no
  environment, so there is nothing to converge in §3 -- it behaves exactly as
  it always has.

Do NOT synthesize a `.toml` from a v1 manifest. That would mean parsing
hand-written prose into a validated schema -- reintroducing the fragility the
TOML exists to remove -- on someone else's published content. A v1 template
becomes v2 when its own publisher next updates it.

**`Requirements` holds two kinds of entry, handled at different times.** They
are not interchangeable, and the kind is a property of the entry rather than of
which section it sits in:

- **Activation** -- the machine-readable `requires_permission:` /
  `requires_secret:` / `requires_llm:` lines, mirrored in the TOML as
  `[[requirements.permission]]`, `[[requirements.secret]]`, and
  `[requirements.llm]`. You ACT ON these yourself, FIRST, before asking the
  user anything.
- **Adaptation** -- the prose bullets, mirrored as
  `[[requirements.adaptation]]`. Design gaps the original author left for you
  to work through interactively WITH the user, after activation.

`Environment` is separate and is not a decision at all: it is what must be
INSTALLED, declared in the TOML's `[environment]` and converged in §3 rather
than resolved by hand.

(An older v1 manifest splits these across `Prerequisites` and `Holes`; read
both, and treat `Prerequisites` as the activation half.)

**`[[lineage]]` is provenance, not work.** It records the templates this one
was built on, each with a repo URL and the exact commit it was used at, because
a new manifest overrides its predecessor rather than accumulating beside it.
Follow a link only if you need to understand where something came from; there is
nothing to adopt there.

## 3. Activate first, then ask how to adapt

In chat, in plain language, walk the user through what this template provides
and what it needs from them — name the activation requirements (do not enumerate
file paths at the user). Then ask whether they want to run it on the same connectors:
"This uses Slack to pull in messages — want me to connect it to your Slack now,
or would you rather it read something else, like email?"

**If they keep the same connectors — set it up BEFORE the adaptation
conversation:**

1. Initiate every activation requirement YOURSELF, now -- one latchkey
   permission request per `requires_permission:` line (see the `latchkey` skill: `latchkey curl -XPOST
   http://latchkey-self.invalid/permission-requests`; the request opens the
   approval/login flow in the minds app). Each request is its own tool call,
   with nothing else in it; when a template needs several, file them one after
   another without waiting for verdicts in between. Do not merely tell the user a
   permission is needed — send the request so it appears for them to approve.
2. **Install what the template declares.** Read `[environment]` in
   `template.toml`. If it is empty, skip this. Otherwise install the entries
   yourself, with the ordinary commands -- there is no special convergence step
   to invoke:

   ```bash
   apt-get install -y --no-install-recommends <apt names>   # names only
   npm install -g <name>@<version>                          # per npm_global entry
   uv tool install <name>==<version>                        # per uv_tools entry
   cargo install --locked <name>@<version>                  # per cargo_crates entry
   ```

   Two things make this simpler than it looks. **apt is already pinned** to this
   workspace's snapshot timestamp, so installing by bare name yields versions
   consistent with the rest of THIS environment rather than the publisher's --
   that is exactly why the manifest declares names and not versions. And you do
   not need to record anything: env-converge captures whatever you install (a
   dpkg hook plus boot-time probes), so it lands in the environment record and
   survives a rebuild or restore on its own.

   Any `env_d_units` the manifest lists are shell scripts already in the tree at
   `system/scripts/env.d/`. Leave them alone -- env-converge runs them on the
   next boot. Run one by hand only if the app needs it working right now.

   If an apt package will not resolve, do not press on into a setup that cannot
   work -- tell the user which package and why:

   - **it does not exist at this workspace's pinned timestamp** (the publisher
     was on a different snapshot) -- offer `uv run env-converge upgrade`, which
     advances this workspace to its committed timestamp;
   - **cargo entries with rust absent** -- an upgrade will not help; rust has to
     be installed first.

3. Wire up any `requires_secret:` values (ask the user for them), start the
   services, and get the app running against THEIR data.
4. **Definition of done for a data-backed app: the user can open it and see
   their OWN data.** A service that starts cleanly or an endpoint that returns
   200 is NOT done — open the app's actual output yourself and confirm it
   shows the user's real content before saying it works.
5. Tell them it is live and invite them to take a look and play with it.

Only then ask: "Now — how would you like to adapt it?"

**If they want different connectors** (e.g. email instead of Slack), skip
activation and go straight to the adaptation conversation — the swap is the
first adaptation, and its new activation requirements get initiated the same way once
decided.

## 4. Resolve requirements interactively

Work through each requirement with the user, one at a time. A requirement is anything the
manifest flags as missing/stubbed, plus any merge conflict from step 1. Translate
each into non-technical terms, ask the user how they want it resolved when you are
unsure, and make the change. Only ask when you genuinely need a decision — resolve
the obvious ones yourself and keep moving.

## 5. Append a dated worksheet entry

The manifest is a worksheet. After adapting, **append** a dated entry to its
`Adaptation history` section — never rewrite the rest of the file. Append only:

```markdown
### <YYYY-MM-DD> — adapted by this mind
<what was changed / which requirements were resolved / decisions made>
```

Earlier history entries are left exactly as they are; each mind that adapts the
template adds one more entry below the previous ones.

## 6. Override and lineage

A mind holds ONE manifest. A merged-in v2 template's `template.md` /
`.toml` / `.svg` **override** whatever was at the repo root before -- they do
not accumulate beside it. The previously-adopted template's *code* stays in
the tree (the merge that brought it in is not undone); only its manifest is
replaced.

So that the override loses nothing, record where this copy came from. After the
merge lands, write an `[origin]` table into the new `template.toml` with the
repo URL and the exact commit you fetched:

```toml
[origin]
repo_url = "https://github.com/<owner>/<repo>"
commit = "<the full sha of FETCH_HEAD you merged>"
adopted_on = "<today, YYYY-MM-DD>"
```

That is the address the NEXT override turns into a `[[lineage]]` entry -- and
what makes the chain in a later published manifest name every template this
mind was built on, each at the commit it was actually used at. Without it the
link is simply lost: nothing else records it.

Keep any `[[lineage]]` entries the incoming manifest already carries; they are
its own ancestry and they come through untouched.

## 7. Commit

Commit the adaptation per the repo's git conventions (a plain local commit;
when the user has enabled GitHub sync, the post-commit hook handles any push).
Include the merged-in tree, the modified
files from resolving requirements, and the updated manifest with its new `Adaptation
history` entry.
