# Uncertainties

Conflicts between documentation and code, noticed while writing specs; resolve and delete entries as they are fixed.

## minds-deployment-tests.md carries superseded deferred/open items

`specs/minds-deployment-tests.md` still lists the pool-host bake/lease/user-isolation test as "deferred to a follow-up PR" (described in OVH-VPS-era terms) and GitHub Actions CI integration as "blocked on solving vault-in-runner".
Both are stale: CI runs the deployment tests via Vault OIDC today (`build-minds-ci-env` in ci.yml), and the pool test is now specified (slice-era) by `specs/remote-workspaces-in-ci.md`.
Noticed while writing that spec; it assumes the newer state is correct.