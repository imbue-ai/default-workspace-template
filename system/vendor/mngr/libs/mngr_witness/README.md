# mngr-witness

The behavior-corpus witness pipeline, designed in `specs/behaviors-mapreduce/spec.md`.

`mngr witness --root <project>/behaviors` runs five nodes over a corpus on the pipeline executor from `libs/mngr_mapreduce`: setup finds or makes the shared test scaffolding, map converges one agent per feature file, review checks each mapper adversarially, integrate cherry-picks the reviewed branches together in the orchestrator, and reduce lets one agent integrate what conflicted, collapse duplication, and write the changelog. Where each node runs, with what environment, is an execution plan built from `--provider`, `--node-provider`, and `--node-env`.

What lives here:

- `pipeline.py`: `make_witness_pipeline`, the witness instance of the declarative pipeline model, and `WITNESS_PIPELINE`, the packaged default. The pipeline carries the prompt templates, so a project's variants make a different pipeline object. The picture in the spec is rendered from the default, so the model is the source of truth for the diagram (`pipeline_test.py` checks).
- `bindings.py`: the behavior attached to the pipeline's names: how each agent node decides its jobs and the values their prompts render from, the integrate node that cherry-picks reviewed branches, and the assembled `PipelineBindings`.
- `gates.py`: the nine mechanical gates run on every agent node's jobs, each a pure function of a branch, an archive, and the corpus. `conventions.py` holds the layout rules they and the prompts share: what counts as a test path, a changelog entry, a project.
- `outcomes.py`: the outcome each node's agent writes and the evidence it ships (junit, witness links, scaffolding guide), with loaders that treat a missing or malformed file as a failure.
- `clauses.py` and `corpus.py`: a unit's clauses (its observable claims) and the feature tasks the corpus selects into, each with an agent-name-safe slug.
- `prompts.py` and `prompt_assets/`: the four node templates and the partials they include. A project variant fills their named blocks by `{% extends %}` from `<project>/witness/<name>.j2`, where `<name>` is `scaffold`, `mapper`, `reviewer`, or `annealer`. Every value a template reads is a string: the run-wide facts are the execution's context, and the bindings render the per-job sections (units, existing witnesses, branches) before handing them over.
- `plan.py` and `agent_type.py`: the execution plan from flags, and the agent type that never pauses for a permission or trust dialog.
- `cli.py`: the `mngr witness` command.

Regenerate the spec diagram after changing the model:

```sh
uv run python libs/mngr_mapreduce/scripts/render_pipeline_svg.py imbue.mngr_witness.pipeline:WITNESS_PIPELINE specs/behaviors-mapreduce/pipeline.svg
```
