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

## Contents

- [next_deploy.md](./next_deploy.md) -- the running checklist for the next
  staging / production deployment (reset after each release ships).
- [release.md](./release.md) -- how to cut a minds release (tag pair, vendor
  sync, launch-to-msg verification, ToDesktop publish).
- [history/](./history/) -- one distilled record per shipped release: what
  was deployed, durable infrastructure coordinates, and lessons learned.
  Add an entry when a deployment concludes.
- Tier operations: [staging-bringup.md](./staging-bringup.md),
  [production-release-deployment.md](./production-release-deployment.md),
  [environments.md](./environments.md), [vault-setup.md](./vault-setup.md),
  [observability-bringup.md](./observability-bringup.md),
  [bugsink-bringup.md](./bugsink-bringup.md).
- Pool / bare-metal operations: [host-pool-setup.md](./host-pool-setup.md),
  [reboot-resilience-rollout.md](./reboot-resilience-rollout.md),
  [slice-hardening-rollout.md](./slice-hardening-rollout.md),
  [slice-restart-wipes-owner-ssh-key.md](./slice-restart-wipes-owner-ssh-key.md),
  [workspace-stop-start.md](./workspace-stop-start.md),
  [lima-image.md](./lima-image.md).
- Incident response: [lost-device-runbook.md](./lost-device-runbook.md).

Note: this folder's *history in the public mirror* predates the exclusion --
files that lived at `apps/minds/docs/*.md` before 2026-08-18 were mirrored,
so treat their pre-move revisions as public.
