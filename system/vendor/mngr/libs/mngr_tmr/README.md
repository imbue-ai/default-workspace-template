# mngr-tmr

Test map-reduce plugin for [mngr](https://github.com/imbue-ai/mngr). It ships two
recipes over the same map -> reduce machinery, differing in their scope anchor:

- `mngr tmr` (docstring-anchored): collects tests via pytest and launches one
  agent per test; each test's docstring is the contract for what it verifies.
- `mngr tmr-behaviors` (behavior-anchored): scans a behavior corpus (see
  `mngr behaviors`, from `imbue-mngr-behaviors`) and launches one agent per
  `.feature` file; each agent creates or updates the tests witnessing that
  file's behavior units, keeping the `witnesses(coordinate, partial=...)`
  markers honest. The corpus itself is read-only to the whole pipeline:
  mappers may only propose behavior edits via the report's
  behavior-escalations section, and an integrated branch that touches the
  corpus is mechanically refused.

Both poll agents to completion, pull successful work into local branches,
integrate them with a reducer agent, and generate an HTML report (the behavior
recipe's report adds a per-coordinate coverage matrix of claimed vs verified
coverage).

Agents report escalations independently of their own outcome, so a passing test can still flag a problem that needs a suite-wide fix. The reducer collapses changes that many agents made identically into one shared fix, writes a single changelog entry for the run, and -- when given a `GH_TOKEN` via `--reducer-env` -- opens the run's pull request, whose description leads with the report link and carries the mapper status breakdown, the reducer's unresolved escalations in full, and its resolved ones one line each. The reducer's escalations are groupings of the test agents', so the per-agent list stays in the HTML report rather than the pull request.

## Variants

A single TMR command can serve distinct test suites as separate, independently
reviewable runs. A variant is just a set of CLI flags:

- `--name <slug>` sets the prefix for the run's agent, branch, and host names
  (e.g. `tmr-mngr` produces `tmr-mngr/<run>/*` branches, `tmr-minds` produces
  `tmr-minds/<run>/*`). This keeps two suites' branches, agents, and PRs
  separate. It is distinct from `--run-name`, which identifies one run within a
  variant.
- The test paths / markers after `--` select which suite runs (the mngr and
  minds suites are separated by path: `libs/...` vs `apps/minds`).
- `--mapper-prompt` / `--reducer-prompt` point a variant at its own Jinja
  prompt templates. An override template may `{% extends %}` or `{% include %}`
  the packaged `mapper.j2` / `reducer.j2` by name to reuse the shared body.
- `--env` supplies any credentials a variant needs.
- `--reducer-env` supplies credentials to the reducer ONLY, never to the
  mappers. `GH_TOKEN` goes here: it is what lets the reducer open the run's PR,
  and the mappers must not hold a token that can push.

Example (two variants):

```bash
mngr tmr libs/mngr  --name tmr-mngr  -- -m "release and not docker and not docker_sdk"
mngr tmr apps/minds --name tmr-minds --mapper-prompt apps/minds/tmr/mapper.j2 -- -m "release and not minds_deployment and not minds_services and not minds_snapshot_resume"
```

Variant definitions live in the caller, not in a registry inside this package.
The canonical flag sets are the `tmr-mngr` / `tmr-minds` / `tmr-behaviors-minds`
just recipes (the `.github/workflows/tmr.yml` workflow inputs mirror the first
two). The minds variants ship minds-tailored mapper prompts under
`apps/minds/tmr/`.

The behavior recipe's variants work the same way, with one refinement: the
packaged `behavior_mapper.j2` defines two named block slots --
`project_guidance` (where new witnessing tests go in the target project's test
taxonomy) and `infra_blockers` (host-capability knowledge) -- so a variant
template `{% extends %}` it and fills the slots instead of forking the contract
body. `apps/minds/tmr/behaviors_mapper.j2` is the exemplar. Fork a
self-contained copy (as the docstring-recipe minds variant does) only when a
variant must *remove* parts of the packaged contract.

## Host cleanup

TMR does not destroy the hosts it creates: they idle-shut-down but keep their
records, and on a shared provider namespace those records accumulate until they
slow host creation to the point of failing it.
`scripts/prune_tmr_hosts.py` sweeps them up. It keeps each variant's most
recent run -- so its mappers stay available to re-attach to for debugging --
plus any run too young to be sure it has finished, and destroys the rest along
with their records:

```bash
uv run --project libs/mngr_tmr python libs/mngr_tmr/scripts/prune_tmr_hosts.py --dry-run
```

CI runs it daily against the shared `tmr-ci` namespace ahead of the scheduled
runs (`.github/workflows/tmr-cleanup.yml`). To run it by hand against that
namespace, point `MNGR_HOST_DIR` at the host dir `scripts/setup_tmr_ci_debug.py`
creates.

A pruned run can no longer be reintegrated: `--reintegrate` rediscovers a run's
mappers by label, and pruning destroys them. Raise `--keep-runs` (in the
workflow's `keep_runs` input) to keep more than the most recent run around.
