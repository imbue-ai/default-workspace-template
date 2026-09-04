---
name: release-minds
argument-hint: <version>
description: Ship a minds release end to end -- cut the minds-v<version> tag pair on mngr and default-workspace-template, rehearse on staging, bake production pool hosts, deploy the tier services, and promote a release channel. This skill owns the order of the steps; each runbook lives in apps/minds/docs/deploy/ops/. Use when asked to "release a new version of minds", "cut a minds release", "bump the minds version", "finish the 0.5.0 release", "deploy minds to production", "bake pool hosts", "promote minds to alpha/beta/stable", "roll out the new minds version", or anything of that shape.
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
ls apps/minds/docs/deploy/history/                              # what was recorded
```

| observed | resume at |
|---|---|
| version not bumped, or a tag missing | **1** |
| both tags exist, no green launch-to-msg on them | **1** |
| pair green, no rehearsal reported | **2** |
| rehearsed, production pool has no rows at the tag | **3** |
| production baked, channel still on the old build | **4** |
| channel moved, no `history/minds-v<version>.md` | **5** |

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

1. Bake at the tag → **pool-hosts.md**, `<tier>` = `staging`. Bake more than you
   will demo; the rehearsal's own testing consumes them.
2. Verify the app → **app-release.md**, *Verifying a release in a running
   workspace*. Put the checkout on the release commit **first**.
3. Retire what you baked, by id → **pool-hosts.md**. Never `pool teardown-slices`
   on a shared tier — it takes no filter.

If anything fails the release is not ready. Fix it and cut the **next** version;
do not move this one's tags.

## 3. Production

**Bake before you deploy.** A deploy freezes the web-create pin from
`FALLBACK_BRANCH`, and browser creates match that tag exactly with no rebuild
fallback, so deploying first breaks them until the bake lands. The desktop is
unaffected — it falls back to a slow rebuild.

1. Size, bake, verify, retire → **pool-hosts.md**, `<tier>` = `production`.
2. Verify by leasing one from the desktop → **app-release.md**, as in step 2 but
   against production's log dir.
3. Deploy the services → **services.md**. Optional: the server tracks `main`, not
   the tag, so deploy only if you want its changes or are heading for beta/stable.

Then tell internal users. Their reports are the last signal before the channel
moves.

## 4. Promote a channel

→ **app-release.md**, step 9. Last, because promotion is what reaches everyone and
`allowDowngrade` is false — a bad build cannot be recalled from installs that took
it. Beta and stable expect the pool already baked at the tag; alpha does not.

## 5. Record it

- **Write `apps/minds/docs/deploy/history/minds-v<version>.md`** — both tag SHAs,
  the build id, what was deployed and its `deploy_id`, the bake per box, what step
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
