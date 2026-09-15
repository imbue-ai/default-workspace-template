---
name: release-minds
argument-hint: <version>
description: Ship a minds release end to end -- cut the minds-v<version> tag pair on mngr and default-workspace-template, rehearse on staging, deploy the tier services, bake production pool hosts, and promote the desktop and web release channels. This skill owns the order of the steps; each runbook lives in apps/minds/docs/deploy/ops/. Use when asked to "release a new version of minds", "cut a minds release", "bump the minds version", "finish the 0.5.0 release", "deploy minds to production", "bake pool hosts", "promote minds to alpha/beta/stable", "roll out the new minds version", or anything of that shape.
---

# Ship a minds release

Minds ships three things on independent cadences. **This skill owns the order;
each runbook owns its own mechanics.** All paths are from the repo root.

| | what it gets you | runbook |
|---|---|---|
| **app release** | a green `(mngr, dwt)` SHA pair, tagged and built into a release candidate | `apps/minds/docs/deploy/ops/app-release.md` |
| **pool hosts** | machines pre-baked at that tag, so a create leases one in ~45s instead of building for ~5min | `apps/minds/docs/deploy/ops/pool-hosts.md` |
| **services** | the imbue cloud services the app talks to — sign-in, workspace leasing, sharing, LLM keys | `apps/minds/docs/deploy/ops/services.md` |

## First: disable the code-guardian stop hook

It merges `origin/main` and *pushes* on every stop — in this repo and in the
default-workspace-template checkout — rewriting the tree a release keeps pinned.

```
/imbue-code-guardian:reviewer-disable
```

Read `.reviewer/settings.local.json` back and report the state you found; it is
gitignored, so an agent worktree only gets a create-time snapshot. Restore with
`reviewer-enable` at wrap-up. Fix any bug found mid-release in a separate
worktree, where the hook stays on and reviews it.

## Then: work out where the release already is

A release spans days, so most requests join one partway through. **Never assume
you are starting at step 1.** Establish state and report it, including the
version — from this skill's `args` if given, else `package.json`:

```bash
grep -m1 '"version"' apps/minds/package.json                    # version being cut
git tag -l 'minds-v*' | sort -V | tail -3                       # cut on mngr?
DWT=${DEFAULT_WORKSPACE_TEMPLATE:-$(sed -n 's/^DEFAULT_WORKSPACE_TEMPLATE_DIR=//p' apps/minds/.env 2>/dev/null)}
git -C "${DWT:?set it, or git -C '' silently reports mngr's tags as dwt's}" \
  tag -l 'minds-v*' | sort -V | tail -3                         # cut on dwt?
gh run list -R imbue-ai/mngr-internal --workflow=minds-launch-to-msg.yml -L 5 \
  --json databaseId,conclusion,createdAt,displayTitle           # pair verified?
eval "$(uv run minds-admin env activate production)" && just pool-list \
  | jq -r '[.[] | select(.attributes.repo_branch_or_tag=="minds-v<version>")] | length'
grep -A4 '\[channels\.' apps/minds/release-channels.toml       # what ships today
grep -A1 '\[web_channels\.' apps/minds/release-channels.toml   # what browser creates pin to
curl -s https://minds-production--rsc-production-api.modal.run/version   # connector deploy_id
ls apps/minds/docs/deploy/history/                              # what was recorded
```

| observed | resume at |
|---|---|
| version not bumped, or a tag missing | **1** |
| both tags exist, no green launch-to-msg on them | **1** |
| pair green, no rehearsal reported | **2** |
| rehearsed, production connector not deployed from this tree | **3** |
| connector deployed, production pool has no rows at the tag | **3**, from its step 2 |
| production baked, desktop channel or web channel still on the old tag | **4** |
| channels moved, no `history/minds-v<version>.md` | **5** |

A launch-to-msg run reporting success in under ~2 minutes was a marker **cache
skip**, not a verification. Check its job durations.

## 1. Cut and verify the pair

→ **app-release.md**, steps 0–8. Produces `minds-v<version>` on both repos, a
ToDesktop build id, and a green launch-to-msg on the pair.

**Treat a tag as immutable once anything has run against it.** Never move
`minds-v<version>` to a different commit — take the next version number instead.
Downstream caches key on the tag *name*, not its content: a box that already
holds that tag's image tar will keep serving the old one, so a later pool bake
reports success while baking the previous code.

## 2. Rehearse on staging

The human gate. CI builds one clean machine and sends it one message; it never
sees an upgraded workspace, a share, a terminal, or latchkey. The 0.4.3 rehearsal
caught a release-blocking bug with every CI gate green.

1. Deploy the services → **services.md**, `<tier>` = `staging`. Same order as
   production, deploy first: the release's clients talk to this server, and the
   rehearsal is what proves the two agree. Staging has no update feed, so the
   deploy also moves its browser-create pin to the new tag at once; the only
   cost is that browser-chrome creates on staging answer 503 until the next
   step lands, which is acceptable there. Do not bake before the deploy.
2. Bake at the tag → **pool-hosts.md**, `<tier>` = `staging`. Bake more than you
   will demo; the rehearsal's own testing consumes them.
3. Verify the app → **app-release.md**, *Verifying a release in a running
   workspace*. Put the checkout on the release commit **first**.
4. Retire what you baked, by id → **pool-hosts.md**. Never `pool teardown-slices`
   on a shared tier — it takes no filter.

If anything fails the release is not ready. Fix it and cut the **next** version;
do not move this one's tags.

## 3. Production

**The order is deploy, then bake, then repoint.** Browser creates
(`/hosts/claim`) match the tag the release feed's `<channel>-web.json` names, with
no rebuild fallback. On production that pin is a release-channel pointer
(`[web_channels.*]` in `release-channels.toml`, moved in step 4), so a connector
deploy does not move it and can safely go first; it also brings the connector
code the new clients expect. The old pin is what browser creates keep leasing
until step 4, so keep `available` rows at the old tag until then.

1. Deploy the services → **services.md**. The server tracks `main`, not the tag;
   a deploy is needed when the release carries connector changes or is heading
   for beta/stable, and is harmless otherwise. Before deploying, confirm every
   `curl -s https://updates.imbueminds.com/<channel>-web.json` (stable, beta,
   alpha) names a tag the pool has `available` rows at — the deployed connector
   leases exactly what those files say — and repoint first if one does not.
   After, confirm `/version` advanced.
2. Size, bake, verify → **pool-hosts.md**, `<tier>` = `production`. Do not
   retire the old generation yet: its `available` rows are what browser creates
   lease until step 4 moves the web channels off its tag.
3. Verify by leasing one from the desktop → **app-release.md**, as in step 2 but
   against production's log dir.

Staging and dev envs have no update feed, so there the deploy-time pin
(`FALLBACK_BRANCH`, or an explicit `MINDS_WEB_TEMPLATE_REF`) is the live one and
a deploy moves browser creates to the new tag at once. The order does **not**
invert: deploy first there too (step 2 above), and accept that browser-chrome
creates on that tier answer 503 until the bake lands.

Then tell internal users. Their reports are the last signal before the channels
move.

## 4. Promote the channels

→ **app-release.md**, step 9 (desktop) and 9b (web). Last, because promotion is
what reaches everyone and `allowDowngrade` is false — a bad build cannot be
recalled from installs that took it. For the desktop, beta and stable expect the
pool already baked at the tag; alpha does not, since a desktop client falls back
to a rebuild. Every web channel does, alpha included: web creates have no rebuild
fallback (2 below).

The two pointers move independently, on their own PRs or one:

1. **Desktop**: point `[channels.<channel>]` at the build.
2. **Web**: point `[web_channels.<channel>]` at `minds-v<version>`. Only after
   step 3's bake landed rows at that tag — web creates lease an exact match and
   the publish checks only that the tag exists on the template remote. Takes
   effect within a few minutes of the merge (the publish, then the feed's and the
   connector's one-minute caches), with no connector deploy. Confirm with
   `curl -s https://updates.imbueminds.com/<channel>-web.json`, then create
   a workspace from the web chrome on that channel and check the connector log
   for `Could not read the web pin` or `No web pin published`.
3. **Retire the old generation** → **pool-hosts.md**, only once no
   `<channel>-web.json` on the feed names its tag (the `curl` above, per
   channel — the connector reads the feed, not the toml, and the feed's copy is
   cached a minute) and a further minute has passed for the connector's cache
   to roll. A channel left behind — stable, during a gradual desktop rollout —
   keeps leasing its tag, so its rows stay.

## 5. Record it

- **Write `apps/minds/docs/deploy/history/minds-v<version>.md`** — both tag SHAs,
  the build id, what was deployed and its `deploy_id`, the bake per box, which
  web channels were repointed and when, what step
  2 verified, what was deferred, and anything that surprised you. Follow the 0.4.x
  entries. This is the only record mapping a `deploy_id` back to a commit.
- **Reset `next_deploy.md`** — discharge what shipped, stamp `Last reset:`. It is
  a queue, not an archive.
- **Fix the runbooks where they were wrong.** You will find something.
- **Re-enable the stop hook.**

## Throughout

Every repo change owes a changelog entry per project it touches
(`<project>/changelog/<branch>.md`) or CI fails the PR. Report the command you
ran and its output rather than that a step "completed": each step states its own
check, and several actions are irreversible — channel promotion,
`pool-destroy`, and `env deploy`'s automatic database rollback.
