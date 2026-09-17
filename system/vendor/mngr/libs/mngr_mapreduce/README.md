# mngr-mapreduce

Map-reduce framework for [mngr](https://github.com/imbue-ai/mngr).

## Pipelines

A `Pipeline` (`pipeline.py`) describes a whole agent pipeline as data: the parameters an execution supplies, the artifacts the operator provides, and the nodes. Nodes carry no order -- each declares the artifacts it `needs`, and execution order is derived from that, so a pipeline is any directed acyclic graph of mappers, reducers and shuffles rather than one fan-out followed by one fan-in. Validators reject a pipeline with a cycle, a dangling dependency, an ambiguous fan-in, or a prompt template that reads a name nothing supplies, so an inconsistent pipeline cannot be constructed and no agent is ever launched for one.

Each node declares how it fans out, and the framework does that arithmetic rather than each plugin:

| `FanoutKind` | Job count | The MapReduce analogue |
|---|---|---|
| `SINGLE` | exactly one | the reducer |
| `PER_UPSTREAM_JOB` | one per successful job of the node producing the artifact it names, minus those the binding declines | a map over the previous node's results |
| `PER_PARTITION` | one per distinct partition key over that same named artifact's results | the shuffle, with a partition function |
| `DISCOVERED` | decided by the binding | the initial map over an input split the pipeline cannot see |

Filtering is fan-out: a `PER_UPSTREAM_JOB` binding returns `None` for results that need no work. When a node fans out to nothing it succeeds vacuously, the way `all([])` is true, and emptiness propagates to a green finish. A node for which finding nothing is instead a mistake says so with `min_job_count`.

A node's prompt is part of the pipeline: it carries the Jinja template its jobs' prompts render from and declares the variables its binding supplies per job. The executor renders the prompt; a binding never composes one.

## Running a pipeline

`executor.py` runs a pipeline under an `ExecutionPlan` (`execution.py`), which says where each node runs and under what limits, and is the only thing that changes between running locally, on Docker and on Modal. Concurrency is structural and parallelism is a cap: every node whose dependencies are met is eligible, and `max_concurrent_nodes` decides how many actually run.

A node succeeds when it meets `required_completion` -- the fraction of its jobs that must succeed, `1.0` by default. A node that falls short halts the whole run, because a partially inconsistent result is worse than a failed one. A job has succeeded when it published an outputs archive, recorded no error, and passed every gate its node declares.

`AbstractPipelineExecutor` owns everything that is the same everywhere -- the scheduling loop, the fan-out arithmetic, job naming, prompt rendering, launching, polling, timeouts, archive and branch collection, gating, node evaluation and halting -- and leaves abstract only how agents get hosts, get launched, and report back. Behavior attaches to the pipeline's names through `bindings.py`, and a pipeline with an unbound node, a binding of the wrong shape, or a plan naming a node the pipeline lacks is refused before any agent launches.

`pipeline_svg.py` renders any pipeline to a deterministic SVG, laid out rank by rank so nodes that can run together share a row; `scripts/render_pipeline_svg.py` is the command-line entry point for drawing one.

Design notes are in [`specs/mapreduce-pipeline-topology/`](../../specs/mapreduce-pipeline-topology/).

## The recipe path

The older `MapReduceRecipe` flow (`cli.py`, `orchestration.py`, `data_types.py`) is unchanged and is what `mngr tmr` runs on. It fans a single recipe-defined task list out to one agent per task, optionally runs a single reducer, and renders a recipe-defined report. See [mngr-tmr](../mngr_tmr/) for the canonical recipe.
