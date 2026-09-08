# Deployment and operations docs (NOT publicly mirrored)

Everything under `apps/minds/docs/deploy/` is **excluded from the public
mirror** (`apps/minds/docs/deploy/**` in `mirror/copy.bara.sky`), unlike the
rest of `apps/minds/**`, which syncs to the public repo wholesale.

That makes this folder the safe place for deployment and operations content:
tier bring-up runbooks, Vault layouts, box/pool operations, release
procedures, incident notes, infrastructure coordinates (addresses, domains,
ticket numbers), and anything else an operator needs but the public repo
should not carry. When writing a doc that touches deployed infrastructure,
default to putting it here.

Do NOT put actual secret *values* anywhere in the repo, this folder included
-- secrets live in Vault. This folder is for the operational knowledge
*around* them (key names, paths, procedures).

## Three lifecycles

Minds ships three things, on three independent cadences. Each has one owning doc
in [ops/](./ops/):

| | ships | owner |
|---|---|---|
| **App release** | a `minds-v<version>` tag pair and a ToDesktop build, promoted to a channel | [ops/app-release.md](./ops/app-release.md) |
| **Pool hosts** | pre-baked machines a workspace create can lease in ~45s | [ops/pool-hosts.md](./ops/pool-hosts.md) |
| **Tier services** | the connector, LiteLLM proxy and analytics Modal apps | [ops/services.md](./ops/services.md) |

Each doc owns its own mechanics, and each says early on how to establish where
things currently stand, so you can join one partway through.

**What sequences them is the `release-minds` skill**
(`.claude/skills/release-minds/SKILL.md`) — it holds the order of the phases, the
couplings between them, and what to do at the end. Read it first when shipping a
release; read the docs above directly when you only need one of the three.

## Contents

**[ops/](./ops/) — recurring.** The three docs above. Start here.

**[setup/](./setup/) — one-time, per environment.** Run once when standing
something up, never during a release:
[tier-bringup.md](./setup/tier-bringup.md),
[vault.md](./setup/vault.md),
[order-boxes.md](./setup/order-boxes.md),
[update-feed.md](./setup/update-feed.md),
[observability.md](./setup/observability.md),
[bugsink.md](./setup/bugsink.md),
[lima-image.md](./setup/lima-image.md).

**[reference/](./reference/) — concepts the ops docs cite.**
[environments.md](./reference/environments.md) (tiers, activation, data roots,
generation ids, dev envs),
[workspace-stop-start.md](./reference/workspace-stop-start.md),
[lost-device-runbook.md](./reference/lost-device-runbook.md).

**[history/](./history/) — what happened.** One entry per shipped release,
written at the end of [ops/app-release.md](./ops/app-release.md). These are also
the only record mapping a connector `deploy_id` back to a commit.
[history/rollouts/](./history/rollouts/) holds finished rollout and incident
write-ups, kept for their lessons rather than as procedure.

**[next_deploy.md](./next_deploy.md) — the running checklist** for the next
deployment. Read it before cutting; reset it after shipping.

Note: this folder's *history in the public mirror* predates the exclusion --
files that lived at `apps/minds/docs/*.md` before 2026-08-18 were mirrored,
so treat their pre-move revisions as public.
