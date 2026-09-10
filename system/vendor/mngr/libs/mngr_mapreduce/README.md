# mngr-mapreduce

Map-reduce framework for [mngr](https://github.com/imbue-ai/mngr).

Fans out a single recipe-defined task list to one agent per task, polls each agent for its outputs archive, optionally runs a single reducer agent that consumes all of the mappers' outputs, and renders a recipe-defined report. The framework is content-agnostic: it knows how to launch, poll, finalize, and report-render, but treats the contents of each agent's `outputs.tar.gz` as opaque. Recipes attach to the framework via two hooks (`on_mapper_finalized`, `on_reducer_finalized`) to interpret what's in the archive.

See [mngr-tmr](../mngr_tmr/) for the canonical recipe (test fan-out and fix integration).

The package also holds the declarative multi-stage pipeline model (`pipeline.py`): stages, steps, artifacts, and gates as validated data, the object a stage executor iterates. `pipeline_svg.py` renders any such pipeline to a deterministic SVG, and `scripts/render_pipeline_svg.py` is the command-line entry point for regenerating a committed diagram from its model.
