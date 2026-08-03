# Plan: `mngr tmr-behaviors` — behavior-anchored test map-reduce

## Simplification memo

De-complect pass over the prior revision of this plan.
Six cuts, each named braid -> cut -> survivor; preserved items listed after so they are not re-litigated.
Intent, scope, and hard constraints are unchanged.

1. **`{coverage, steady}` pair + cross-field validators -> one 4-state `verdict` enum.**
   Braid (data+representation): two fields represent 6 combinations for 4 legal states, with validators patching the gap (`full` forces `steady=True`, `none` forbids it) — legality policy scattered into runtime checks.
   Cut: `verdict in {NONE, PARTIAL_IMPROVABLE, PARTIAL_STEADY, FULL}`; the matrix-vocabulary `coverage` becomes a pure projection (strip the qualifier) where needed for claimed-vs-verified comparison.
   Survivor: the type — invalid states unrepresentable; the projection function.
   The earlier "decomplected axes" intent survives: blockers and behavior_problems stay separate fields (genuinely independent axes); steadiness was never independent of coverage (meaningful only within partial), so fusing these two is un-braiding.
2. **`recipe_common.py` bucket module -> promote in place.**
   Braid (fused-to-reduce-parts): one "common" module fusing click parsing, git bundle glue, and name validation — unrelated concerns bucketed to reduce file count.
   Cut: no new module; promote privates to public where each concern lives (`recipe.py`: name validation, `apply_branch_bundle`, reducer-branch emit; `cli.py`: `SplitTestingFlagsCommand`; `prompts.py`: `resolve_template`, `PUBLISH_OUTPUTS_SNIPPET`), and the `behavior_*` modules import from those homes.
   Survivor: public names as the explicit contract.
   The future `mngr_mapreduce` factoring stays equally mechanical.
3. **Filter-semantics hedge -> import layer-1 matchers.**
   Braid (policy scattered): the plan hedged "reuse if public, else local `_unit_matches_filters`", risking a recipe-side copy of `list`-filter semantics.
   Verified fact: `behavior_unit_matches_area/tag/name_substring/step_substring` are public in `imbue.mngr_behaviors.corpus`.
   Cut: compose those directly (kind filter is an equality check); delete the contingency and its open question.
   Survivor: layer 1 remains the sole owner of filter policy.
4. **`corpus_violation.json` marker file -> derive at read.**
   Braid (value+time): the gate verdict computed once at finalize and stored as a file — a second source of truth beside git.
   Cut: one helper `corpus_touching_paths(branch, corpus_root)` (merge-base diff); `on_reducer_finalized` calls it for the emit decision, `render_report` calls it for the banner (same stateless pattern as the existing `_reducer_branch_applied`).
   Survivor: git as the single fact source + the shared helper.
   The marker-file concept disappears.
5. **Empty `test_roots` as default sentinel -> resolve at the CLI boundary.**
   Braid (data+representation): empty tuple as in-band "caller chose nothing" marker, with defaulting policy re-applied at use sites ("effective test roots").
   Cut: the CLI resolves the default (`corpus_root.parent`) once; the recipe field is always non-empty (model validator).
   Survivor: the boundary resolution + the validator; use sites read the field directly.
6. **Self-contained minds variant -> `{% extends %}` with named blocks** (a deliberate reversal of an earlier settled decision).
   Braid (policy scattered): a full copy of the mapper contract — the system's objective function, including the outcome schema `behavior_report.py` parses — kept in sync by hand across two projects.
   The TMR-minds precedent forked because its delta was subtractive (dropping tutorial sections); this variant's delta is purely additive, which is exactly what block slots model.
   Cut: the packaged template defines `{% block project_guidance %}` and `{% block infra_blockers %}` with generic defaults; the minds variant extends it and fills the two slots.
   Survivor: the single contract body + the CI render test of the committed variant.
   If a subtractive delta ever appears, fork honestly at that point (extends -> copy is a trivial paste; copy -> extends never happens).

Preserved as intentional: the two-layer outcome (task-level `changes` for reducer mechanics vs per-coordinate `units` for coverage — distinct consumers, not duplication); `tests_passing_before/after: bool | None` (callers branch on None: a vacuous before-state bypasses the regression clause; TMR parity); per-task corpus re-scan in `build_mapper_prompt` (derive-at-read statelessness; corpus parse is milliseconds); reuse of TMR outcome filenames, `ReportSection`, and report css/js chrome (single placement of shared representation); two typed discovery errors (distinct remediation); `matrix.jsonl` presence as the verified-coverage source (derive-at-read from the artifact; absence rendered honestly).
Inherent complexity left alone: the click options-bag mirroring, the bundle/publish protocol, the reducer being a remote agent that runs the CLI rather than library calls.

## Overview

- Build a second map-reduce recipe in `libs/mngr_tmr`, sibling to `TestMapReduceRecipe`: one mapper agent per behavior `.feature` file, each making the tests that witness that file's behavior units converge to the units' scope; a reducer integrates and normalizes, exactly as TMR does today.
- The behavior source is `mngr behaviors` (the `libs/mngr_behaviors` library, a fixed layer-1 interface): `scan_corpus` and the public filter matchers at discovery, the `mngr behaviors` CLI on agent hosts for self-serve detail and verification.
  The caller varies only the corpus root and prompt/justfile variant choices.
- The anchor generalizes TMR's lineage (tutorial block -> docstring -> behavior unit): the unit's steps plus its in-scope invariant Rules define scope; forward, every observable claim is verified by some witnessing test; backward, every assertion traces to a step or an in-scope invariant, else it is gold-plating to remove.
  Creation is not a mode — an unwitnessed unit is the far-from-fixed-point case of the same objective.
- Key settled decisions:
  - Placement/name: recipe modules live in `libs/mngr_tmr` as clearly-separated `behavior_*` files; top-level command `mngr tmr-behaviors`; `mngr_tmr` gains the `imbue-mngr-behaviors` dependency; `libs/mngr_behaviors` is untouched (its PyPI trajectory stays clean).
  - Fan-out: per-`.feature`-file (flexible by design — outcomes are keyed per coordinate, so re-partitioning later touches only `discover()`'s grouping and task ids).
    `invariants.feature` is a task like any other; standalone Rules get direct witnesses.
  - Outcome schema: change kinds `{CREATE_TEST, IMPROVE_TEST, FIX_TEST, FIX_IMPL}` (no behavior-edit kind exists, structurally) plus per-coordinate verdict records: `verdict` (4-state enum below), `witnesses`, `blockers`, `behavior_problems` (the only corpus-change channel — proposed edits ride the outcome JSON, never the tree).
    Honest-partial: `PARTIAL_STEADY` is legitimate iff the `partial=` notes name residue untestable in kind (e.g. universally quantified interleavings); expensive-but-testable residue is `PARTIAL_IMPROVABLE`.
  - Read-only corpus enforcement: prompt instruction (L1) plus a Python egress gate in `on_reducer_finalized` (L3) that refuses to emit a reducer branch whose diff touches the corpus root.
    Corpus `validate` fail-fasts in `discover()`; the matrix link-check runs at normalize.
    No `should_pull` corpus clause.
  - v1 discovery is `scan_corpus` only — no witness harvesting up front; mappers self-collect on-host; the report's verified coverage comes from the reducer's normalize-stage `mngr behaviors matrix` artifact.
  - Conceptual integrity: task ids are root-relative `.feature` paths; `display_id`s are folder-qualified dotted names (`browser-authorization.signin`) mirroring coordinate style; outcome filenames reuse TMR's (`testing_agent_outcome.json` / `integrator_outcome.json`).
- Out of scope, explicitly: CI workflow wiring (`tmr.yml`); the real fleet run (happens on a subsequently stacked branch); any change to `apps/minds/behaviors/`, the behaviors skill, or the layer-1 CLI.

## Expected behavior

- `uv run mngr tmr-behaviors --root apps/minds/behaviors [--tests PATH ...] [--area X] [--tag X] [--unit KIND] [--name SLUG] [--mapper-prompt F] [--reducer-prompt F] [framework opts] [-- flags]` runs a full map-reduce over the corpus at `--root`.
  When `--tests` is omitted the CLI resolves it to the corpus root's parent (matrix's convention) before the recipe is constructed.
- Discovery validates the corpus with `scan_corpus` and aborts (before any agent launches) on violations or zero units; `--area`/`--tag`/`--unit` filter units via the layer-1 matchers (AND-composed, selection-only); remaining units group by `.feature` file into tasks in corpus order.
  An empty post-filter selection is an error.
- One mapper per feature file receives a lean prompt: the file path, a table of its units (coordinate, kind, name, line, parent, in-scope invariant coordinates), `mngr behaviors` self-serve commands, and the contract (behavior-as-anchor forward/backward convergence, witnesses-marker honesty with the verdict vocabulary, corpus read-only, `FIX_IMPL` vs `FAILED`+`behavior_problems` arbitration, commit discipline `[KIND] coords: summary`, outcome JSON schema).
  The mapper reads `.feature` files and sidecars on-host, runs the specific witness tests it touches, and publishes outcome + branch bundle like TMR.
- "No change needed" (empty `changes`, all units at their fixed point) and deletion of gold-plating (credited under `IMPROVE_TEST`) are first-class outcomes.
  Every touched test carries correct `witnesses` markers with `partial=` set or cleared honestly.
- The reducer integrates exactly as TMR (should_pull predicate unchanged; squash `CREATE_TEST`/`IMPROVE_TEST`/`FIX_TEST` into one commit; cherry-pick `FIX_IMPL` individually by priority), then normalizes: dedupes fixtures/scaffolding parallel mappers created independently (two-sided value check; never a unit's observed behavior itself), triages `FIXME(tmr-behaviors)` blockers into normalizations/escalations, runs `mngr behaviors matrix` on the integrated tree, and ships `matrix.jsonl` in its outputs.
- On the operator machine, `on_reducer_finalized` applies the reducer bundle, then consults `corpus_touching_paths(branch, corpus_root)`: empty -> the reducer-branch event is emitted as today; non-empty -> the event is not emitted, and the report (deriving the same fact at render time) shows a loud violation banner with the offending paths.
- The HTML report shows TMR-style task rows plus two new sections: a per-coordinate coverage matrix (mapper-claimed verdict -> reducer-verified coverage from the matrix artifact, with witnesses, partial notes, blockers) and a behavior-escalations section aggregating `behavior_problems` with proposed edits.
  Report renders incrementally during the run and uploads via the existing S3 helper.
- `just tmr-behaviors-minds [args]` invokes the minds variant: `--root apps/minds/behaviors --name tmr-behaviors-minds --mapper-prompt apps/minds/tmr/behaviors_mapper.j2`, no testing flags after `--` (behavior mappers are node-id-directed; host-capability guardrails live in the variant's `infra_blockers` block).
- Existing `mngr tmr` behavior is unchanged.

## Implementation plan

### Public promotions (in place; keeps TMR green)

- `libs/mngr_tmr/imbue/mngr_tmr/recipe.py` (modify): promote the recipe-name validation, `_apply_branch_bundle` -> `apply_branch_bundle`, and the reducer-branch emit helper to public names (second consumer arriving).
- `libs/mngr_tmr/imbue/mngr_tmr/cli.py` (modify): `_TmrCommand` -> `SplitTestingFlagsCommand`.
- `libs/mngr_tmr/imbue/mngr_tmr/prompts.py` (modify): `_resolve_template` -> `resolve_template`, `_PUBLISH_OUTPUTS_SNIPPET` -> `PUBLISH_OUTPUTS_SNIPPET`; outcome filename constants importable.
- `libs/mngr_tmr/imbue/mngr_tmr/report.py` (modify): promote to importable any model/loader the behavior report reuses (`ChangeStatus`, `Change`, `ReportSection`, `IntegratorResult`, `TestRunInfo`, the integrator-outcome loader).

### Recipe

- `libs/mngr_tmr/imbue/mngr_tmr/behavior_recipe.py` (new):
  - `BehaviorMapReduceRecipe(MapReduceRecipe, FrozenModel)` with fields: `name` (default `"tmr-behaviors"`, validated via the promoted validator), `corpus_root: Path`, `test_roots: tuple[Path, ...]` (validator: non-empty; the CLI resolves the default), `area: str | None`, `tag: str | None`, `unit_kind: BehaviorUnitKind | None`, `testing_flags: tuple[str, ...]`, `mapper_prompt_path: Path | None`, `reducer_prompt_path: Path | None`.
  - `discover(ctx)`: `scan_corpus(ctx.source_dir / corpus_root)`; violations -> raise `BehaviorCorpusInvalidError` (fail-fast, listing them); filter units by composing `behavior_unit_matches_area`/`behavior_unit_matches_tag` (from `imbue.mngr_behaviors.corpus`) and kind equality; group units by root-relative file -> `MapReduceTask(id="browser-authorization/signin.feature", display_id="browser-authorization.signin")`; empty selection -> `NoBehaviorUnitsError`.
  - `build_mapper_prompt(ctx, task)`: re-scan corpus (stateless-recipe pattern), select the task file's units, compute `binding_invariant_coordinates` per unit plus a coordinate -> Rule-name context table, delegate to `build_behavior_mapper_prompt`.
  - `build_reducer_prompt(ctx)`: delegate to `build_behavior_reducer_prompt` (corpus root, test roots, template override).
  - `on_mapper_finalized`: `apply_branch_bundle` unconditionally (TMR parity).
  - `on_reducer_finalized`: apply bundle; if `corpus_touching_paths(branch, corpus_root)` is empty, emit the reducer-branch event; otherwise skip the emit (the report derives the same fact).
  - `corpus_touching_paths(...)`: module-level helper — `git merge-base HEAD <branch>` then `git diff --name-only <mb>..<branch> -- <corpus_root>`; returns the touched paths.
  - `render_report`: delegate to `generate_behavior_html_report` (which receives the gate helper's result) + `maybe_upload_report`.

### Outcome models and report

- `libs/mngr_tmr/imbue/mngr_tmr/behavior_report.py` (new):
  - `BehaviorChangeKind(UpperCaseStrEnum)`: `CREATE_TEST, IMPROVE_TEST, FIX_TEST, FIX_IMPL`.
  - `BehaviorUnitVerdict(UpperCaseStrEnum)`: `NONE, PARTIAL_IMPROVABLE, PARTIAL_STEADY, FULL` — the 4 legal end states, invalid combinations unrepresentable; `coverage_of(verdict)` projects onto matrix vocabulary (`none`/`partial`/`full`) for claimed-vs-verified comparison.
  - `WitnessClaim(FrozenModel)`: `node_id`, `partial: str | None`.
  - `BehaviorProblem(FrozenModel)`: `problem`, `proposed_edit`.
  - `UnitVerdictRecord(FrozenModel)`: `coordinate`, `verdict: BehaviorUnitVerdict`, `witnesses: tuple[WitnessClaim, ...]`, `blockers: tuple[str, ...]`, `behavior_problems: tuple[BehaviorProblem, ...]`, `summary_markdown`.
  - `BehaviorTaskResult(FrozenModel)`: `changes: dict[BehaviorChangeKind, Change]`, `units: tuple[UnitVerdictRecord, ...]`, `errored`, `tests_passing_before/after: bool | None`, `test_runs`, `summary_markdown`; parser mirroring the TMR outcome parser.
  - Reducer outcome: TMR's `IntegratorResult` schema unchanged; verified coverage is read from the reducer's `matrix.jsonl` artifact when present (thin loader; absent -> "verified" column shows unavailable).
  - `generate_behavior_html_report(...)`: renders `behavior_report_assets/behavior_report.html.j2`; section derivation reuses `ReportSection` (all three test kinds -> non-impl bucket); static assets (`report.css`, `artifacts.js`) shared from the existing `report_assets` package.
- `libs/mngr_tmr/imbue/mngr_tmr/behavior_report_assets/behavior_report.html.j2` (new): TMR-style task rows + the coverage-matrix section (coordinate, kind, claimed verdict, verified coverage, witnesses with partial notes, blockers) + the behavior-escalations section (aggregated `behavior_problems` with proposed edits) + normalizations/escalations panels + the corpus-violation banner.

### Prompts

- `libs/mngr_tmr/imbue/mngr_tmr/behavior_prompts.py` (new): `build_behavior_mapper_prompt(...)` / `build_behavior_reducer_prompt(...)` rendering the packaged templates via `resolve_template` (ChoiceLoader override support, identical to TMR); context includes corpus root, feature path, unit table, in-scope invariants, `mngr behaviors` command strings, testing flags, outcome filenames, publish snippet.
- `libs/mngr_tmr/imbue/mngr_tmr/prompt_assets/behavior_mapper.j2` (new): the generic mapper contract — behavior unit as anchor; corpus READ-ONLY with no exceptions; forward/backward convergence with invariants as legitimate trace targets; witnesses-marker honesty in the verdict vocabulary (`PARTIAL_STEADY` only for residue untestable in kind); creation as the same objective; `FIXME(tmr-behaviors):` channel; one commit per kind, `[KIND] coords: summary`; outcome JSON schema (changes + units[] + errored + tests_passing_* + test_runs + summary); publish snippet; no-user-input.
  Defines two named block slots with generic defaults: `{% block project_guidance %}` (test-placement: "follow the target project's test taxonomy") and `{% block infra_blockers %}`.
- `libs/mngr_tmr/imbue/mngr_tmr/prompt_assets/behavior_reducer.j2` (new): discover/fetch qualifying branches (TMR's should_pull verbatim); cherry-pick strategy (squash test kinds, `FIX_IMPL` by priority); normalize — scaffolding dedup with the two-sided value check, `FIXME(tmr-behaviors)` triage into normalizations/escalations, `mngr behaviors matrix --root ... --tests ... > matrix.jsonl` (link errors are failures to fix or escalate), blast-radius test verification; outcome JSON (IntegratorResult schema); publish snippet including `matrix.jsonl`.

### CLI and plugin

- `libs/mngr_tmr/imbue/mngr_tmr/behavior_cli.py` (new): `BehaviorTmrCliOptions(MapReduceCliOptions)` (`name`, `mapper_prompt`, `reducer_prompt`, `root`, `tests`, `area`, `tag`, `unit_kind`, `testing_flags`); `tmr_behaviors` click command (cls=`SplitTestingFlagsCommand`) stacking recipe options + `add_mapreduce_options` + `add_common_options`; `--root` required `click.Path(exists=True, file_okay=False)`; `--tests` `multiple=True`, resolved to `(root.parent,)` when omitted, before recipe construction; builds `BehaviorMapReduceRecipe`, calls `run_mapreduce`.
- `libs/mngr_tmr/imbue/mngr_tmr/plugin.py` (modify): `register_cli_commands` returns `[tmr, tmr_behaviors]`.
- `libs/mngr_tmr/pyproject.toml` (modify): add `imbue-mngr-behaviors==0.1.0` dependency + `[tool.uv.sources]` workspace entry.
- CLI docs: regenerate via `uv run python scripts/make_cli_docs.py` (adds the `tmr-behaviors` page; add the command to the script's list if it enumerates explicitly).

### Minds variant and justfile

- `apps/minds/tmr/behaviors_mapper.j2` (new): `{% extends "behavior_mapper.j2" %}` filling the two slots — `project_guidance`: the placement frame (witnesses default to integration `test_*.py`; release when the flow needs the full stack; unit `_test.py` when observable in-process; acceptance reserved for core flows); `infra_blockers`: minds host capabilities (Docker daemon, secrets, `minds_deployment`/`minds_services`/`minds_snapshot_resume`), mirroring the existing minds TMR variant's section.
  Nothing else — the contract body lives only in the packaged template.
- `private.just` (modify): add `tmr-behaviors-minds *args:` -> `uv run --project libs/mngr_tmr mngr tmr-behaviors --root apps/minds/behaviors --name tmr-behaviors-minds --mapper-prompt apps/minds/tmr/behaviors_mapper.j2 {{args}}` (no `--` testing flags), next to `tmr-minds` with a comment.
- `libs/mngr_tmr/README.md` (modify): document the second recipe and its variant flags under the existing Variants section, including the extends-with-blocks variant mechanism.

### Changelogs (one per touched project)

- `libs/mngr_tmr/changelog/danver-behaviors-tmr.md` — the recipe, CLI, prompts, report, egress gate.
- `apps/minds/changelog/danver-behaviors-tmr.md` — the minds variant prompt.
- `libs/mngr/changelog/danver-behaviors-tmr.md` — regenerated CLI docs page.
- `dev/changelog/danver-behaviors-tmr.md` — the private.just recipe and this blueprint.

## Implementation phases

1. **Public promotions**: rename the privates gaining a second consumer (`recipe.py`, `cli.py`, `prompts.py`, `report.py`); update TMR-internal references; full `libs/mngr_tmr` tests stay green.
   (TDD: existing tests are the harness.)
2. **Recipe core**: `behavior_recipe.py` discovery — corpus fail-fast, layer-1 matcher composition, per-file grouping, dotted display ids — with unit tests against fixture corpora (`write_behavior_corpus`) and one test against the live `apps/minds/behaviors` corpus when present (the corpus lands on a sibling branch; the test skips when it is absent).
3. **Outcome models**: `behavior_report.py` enums + models + parsers, unit-tested (verdict parse rejection of invalid values, malformed-JSON handling, `coverage_of` projection).
4. **Prompts**: packaged `behavior_mapper.j2`/`behavior_reducer.j2` + `behavior_prompts.py` builders; tests assert contract passages (read-only corpus, forward/backward, steady semantics), context rendering, block defaults, and override/`{% extends %}` resolution.
5. **CLI + plugin**: `behavior_cli.py`, plugin registration, `--` split, `--tests` default resolution, option threading into the recipe; regenerate CLI docs; tests mirror `cli_test.py`/`plugin_test.py`.
6. **Egress gate + report**: `corpus_touching_paths` + `on_reducer_finalized` wiring with temp-git-repo tests (clean branch -> event emitted; corpus-touching branch -> no event, banner rendered); `generate_behavior_html_report` + template with tests (coverage matrix, escalations, violation banner, matrix.jsonl join).
7. **Minds variant + wiring**: `apps/minds/tmr/behaviors_mapper.j2` (extends; render test asserting the filled blocks and the inherited contract), private.just recipe, README, changelogs.
8. **Finish line**: ratchet snapshot trims where counts drop; `just test-offload` full suite green; manual verification (`uv run mngr tmr-behaviors --help`, `just -n tmr-behaviors-minds`, prompt-render spot-check); commit.
   PR happens only when the code is complete, running, and tested.

## Testing strategy

- **Unit tests (`*_test.py`, colocated in `libs/mngr_tmr`)**:
  - `behavior_recipe_test.py`: name validation; discovery grouping, ordering, and dotted display ids; filter threading and AND-composition (semantics themselves are layer 1's, already tested there); fail-fast on corpus violations and on empty selection; live-corpus discovery snapshot (skips when the corpus is absent); `corpus_touching_paths` and the emit decision both ways in a temp git repo.
  - `behavior_report_test.py`: enum/model round-trips; invalid verdict values rejected at parse; outcome parsing (valid, missing fields, malformed); `coverage_of` projection; section derivation; HTML generation (rows, claimed/verified columns, behavior-escalations, violation banner, HTML escaping).
  - `behavior_prompts_test.py`: mapper prompt contains file path, coordinates, invariant table, self-serve commands, read-only language, verdict vocabulary; block defaults render when no variant is given; reducer prompt contains should_pull, matrix command, `matrix.jsonl` publish; override resolution incl. `{% extends %}`; the committed `apps/minds/tmr/behaviors_mapper.j2` renders with its blocks filled and the contract body inherited.
  - `behavior_cli_test.py` + `plugin_test.py` update: two commands registered; help surface; `--` split; `--tests` repeatability and default resolution; option -> recipe threading.
- **Fixtures**: reuse `imbue.mngr_behaviors.testing.write_behavior_corpus` and existing shared conftest fixtures; no new test-utility tests.
- **Ratchets**: honor in spirit; trim inline snapshots where counts drop; no evasions.
- **Edge cases covered**: corpus with violations; empty corpus; filters selecting nothing; a task file with only Rules (`invariants.feature`); reducer outcome without `matrix.jsonl`; corpus-touching reducer branch; invalid verdict strings.
- **Explicitly not tested here**: real fleet runs (stacked branch), tmux/interactive flows, CI workflow wiring.
- **Full-suite gate**: `just test-offload` green before the branch is done; acceptance tests run in CI.

## Open questions

- Minds variant block wording will be tuned empirically during the stacked-branch real run; the initial blocks mirror the existing minds TMR variant's guidance.
- Whether `--reintegrate` needs any behavior-recipe-specific handling beyond the framework's (expected: none; verify during the real run).
- Report visual polish (column widths, matrix grouping by area) once real artifacts exist.
