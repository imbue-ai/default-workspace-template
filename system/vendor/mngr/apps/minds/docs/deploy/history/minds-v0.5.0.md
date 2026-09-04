# minds-v0.5.0 (2026-09-02/03): baked to production, promoted to alpha

Tag pair: mngr `eee262fd2e`, default-workspace-template `a38e73943e`, both
`minds-v0.5.0`. ToDesktop build `260902shwco3ynx`.

The first release cut without Josh driving the pool bake, so the runbook was
being executed and corrected at the same time; the corrections are in
[../ops/pool-hosts.md](../ops/pool-hosts.md) and its git history.

## How the pair was made

The green pair was *manufactured*, not searched for: bump 0.5.0 and pin
`FALLBACK_BRANCH`, sync `system/vendor/mngr` into dwt, verify the vendor tree
equals `git archive` of the mngr commit, land by fast-forward, tag both repos,
then run launch-to-msg **on the tags**. Green on run `33691585427` -- a real
13-minute run, not a marker-cache skip (the cache keys on
`sha256(mngr_sha:dwt_sha)`, so a sub-2-minute pass means it never ran).

## Staging

Rehearsed end to end and passed: provider-chooser sign-in, terminal, sharing,
the workspace dropdown, latchkey, and an existing workspace's upgrade path.

## Production pool

15 slices baked at `minds-v0.5.0` -- 7 US-EAST-VA, 8 US-WEST-OR -- all verified
container-side, then one leased from the desktop to prove the fast path.

Retired 25 `minds-v0.3.17` rows, filtered to `available` **and** never-leased
(`leased_to_user`, `leased_at`, `released_at` all null). Nothing at
`minds-v0.4.3` was touched, which matters for the pin below.

## Channel

Alpha only, at 100% (PR #809, publish run `33734165269`). Beta and stable stay
at 0.4.2 build `260825un55i8ix7`; the publish run reported both as "nothing to
do". `_DEFAULT_TARGET_BY_PLATFORM` was deliberately not bumped -- it backs the
public download link and must not lead stable, since `allowDowngrade` is false.

## Not done: the production services deploy

Production still runs connector `dabb19b95b`, whose tree carries
`FALLBACK_BRANCH = "minds-v0.4.3"`. `MINDS_WEB_TEMPLATE_REF` is frozen from that
at deploy time, so **browser creates (`/hosts/claim`) still pin to
`minds-v0.4.3`** while desktop 0.5.0 clients ask for `minds-v0.5.0`. The two
create paths therefore target different tags until services are deployed.
Keep `available` rows at `minds-v0.4.3` until then: `/hosts/claim` matches the
tag exactly and has no rebuild fallback.

## Notes

- The provider-chooser sign-in failures seen during the rehearsal were fixed by
  `1ad43000ec` (MIND-237, lease/auth retry, PR #789). An early theory that they
  were mngr/dwt vendor skew was wrong -- a run on a provably matched pair failed
  the same way, which is what disproved it.
