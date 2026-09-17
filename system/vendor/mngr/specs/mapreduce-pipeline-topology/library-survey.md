# Should the mngr-mapreduce pipeline layer be built on an existing workflow library?

Investigation notes, 2026-09-11. Written at Josh's request before speccing the arbitrary-topology
pipeline model, to establish whether an off-the-shelf orchestrator (his example: Airflow) should
carry the topology and hand execution to mngr, rather than us building the layer ourselves.

**Answer: no. Build the model; take the DAG core from `graphlib` in the Python standard library.**

## Method

Read the code that would be replaced: `pipeline.py`, `pipeline_svg.py` and `primitives.py` on
`main`, plus `executor.py`, `data_types.py`, `interfaces.py` and `bindings.py` on the unmerged
`danver/mapreduce-pipeline-executor` branch (2 commits, +2266 lines). The discriminating
requirements below are derived from that code.

Measured every candidate's transitive dependency count with
`uv pip compile --python-version 3.12` against a clean cache, one package at a time, today.
The comparison point is `imbue-mngr` itself, which resolves to **67** packages.

## What the layer has to do

Seven requirements, taken from the code. Only R1, R2, R4 and R5 discriminate between candidates;
R3 and R6 are widely supported. R7 is met by nothing, including this design.

**R1 — The topology has to be an inspectable value, not a traced execution.**
`Pipeline` is a frozen pydantic model. `render_pipeline_svg(pipeline)` draws it without running
anything, and `scripts/render_pipeline_svg.py` resolves a `module:attribute` reference to one.
`assert_pipeline_is_bound` and `assert_plan_fits_pipeline` reject a malformed pipeline *before any
agent launches*. The stated goal is that other Imbue engineers construct these objects in their
own plugins. Any framework that recovers the graph by tracing a decorated function, or by static
analysis of a subclass's source, forecloses all of that.

**R2 — A node is a whole provisioned machine running a coding agent for minutes to hours.**
`AbstractPipelineExecutor` has exactly seven abstract methods, and they are the entire
mngr-specific surface: `provision_hosts`, `launch_agent`, `is_published`, `pull_outputs`,
`fetch_branch`, `stop_agent`, `release_hosts`. No workflow engine models this. Every one of them
models "call this Python function" or "run this container image to completion." Whichever library
we pick, we write those seven methods anyway, as a custom operator or executor plugin.

**R3 — Edges carry git branches and directories, not in-memory Python values.**
`StageJob.base_ref` is a commit or branch; `JobResult.archive_dir` is a filesystem path;
`is_branch_applied` records that a bundled branch was fetched into the operator checkout. This
matches artifact/target-oriented systems and mismatches value-passing ones.

**R4 — Placement is chosen per stage at runtime, from a topology held constant.**
`ExecutionPlan.placement_by_stage_name` maps stage name to `StagePlacement`, which carries
provider, agent type, `env_options`, templates, `agents_per_host`, `max_parallel_launch`,
`max_running_agents` and `agent_timeout_seconds`. Several libraries have a surface-level analogue
(Beam runners, Snakemake `--executor`, Metaflow `--with kubernetes`, Covalent per-task executors,
ZenML stacks) — but in all of them the swappable thing is *where your Python runs*. None of them
has a place to put "this stage's agents get a push-capable token and the mapper stage must not",
which is the reason `reducer_env_options` exists today.

**R5 — This ships inside a wheel end users `pip install`.**
`libs/mngr_mapreduce/pyproject.toml` publishes `imbue-mngr-mapreduce`, packaging only `imbue`.
A dependency added here is a tax on every mngr install. The repo also runs a supply-chain
cooldown (`exclude-newer = 2026-07-20`), so a new dependency is a standing maintenance item.

**R6 — Fan-out width is unknown until the previous stage finishes.**
`AgentStepBindingInterface.discover_jobs(execution) -> list[StageJob]`. Every modern orchestrator
supports dynamic fan-out, so this does not discriminate.

**R7 — A run is long and expensive to lose.**
`ManifestWriter` serialises the whole `Execution` to `execution.json` after every change. Nothing
reads it back yet, so today an interrupted run is a lost run. This is the one requirement where a
library offers something we have not already built.

## Candidates

Dependency counts are resolved package counts, measured as described above.

| Candidate | Deps | R1 topology as data | R2 node = agent host | R4 per-stage placement | R5 shippable | Verdict |
|---|---|---|---|---|---|---|
| Apache Airflow | **127** | Partial, scheduler-bound | No | Weak | No | Reject |
| Prefect | **105** | No — graph traced by running the flow | No | Weak | No | Reject |
| Flytekit | **94** | Partial | No | Via k8s only | No | Reject |
| Covalent | **75** | No — decorator DSL | No | **Yes, its headline feature** | No | Reject, steal the idea |
| Dagster | **48** | **Yes** — `GraphDefinition` is constructible | No | Weak | No | Reject |
| Kedro | **40** | **Yes** — `Pipeline` is a real data object | No | Partial, in-process runners | No | Reject, closest cousin |
| ZenML | **37** | No | No | **Yes, "stacks"** | No | Reject |
| Apache Beam | **32** | Yes, as a graph | No | **Yes, runners** | No | Reject |
| Metaflow | **12** | **No — graph must be statically parseable from source** | No | **Yes, `--with`** | Borderline | Reject, best analogue |
| DBOS | **11** | N/A — durable execution, not topology | N/A | N/A | Borderline | Park for R7 |
| Hamilton | **9** | No — graph from function signatures | No | No | Yes | Reject |
| Luigi | **8** | Partial, target-oriented | No | No | Yes | Reject, weak dynamic fan-out |
| Temporal (`temporalio`) | **5** | N/A — durable execution | N/A | N/A | Needs a server | Park for R7 |
| networkx / paradag | **1** | Graph algebra only | N/A | N/A | Yes | Unnecessary, see below |
| `graphlib` (stdlib) | **0** | Graph algebra only | N/A | N/A | **Yes** | **Adopt** |

- **Airflow** fails on two independent counts. Architecturally, Airflow 3 requires a metadata database,
  a scheduler, a standalone DAG processor and an API server; it is not embeddable as a library,
  and 3.x moved further in that direction, not less. Numerically, it resolves to 127 packages
  against mngr's own 67 — adopting it would nearly triple the install.
- **Metaflow** is the only real contender on paper: 12 dependencies, `foreach`/`join` *is*
  map/reduce, and `--with kubernetes` *is* runtime placement. It fails R1. Metaflow requires
  transitions to be statically parseable from the source of a `FlowSpec` subclass so the graph can
  be translated for runtimes that only accept static graphs. A plugin author could not construct a
  topology as a value; we would lose the SVG, the pre-flight binding check, and the
  pipeline/plan split. Its `foreach`/`join` and `merge_artifacts` semantics are still worth reading.
- **Covalent** and **ZenML** each independently invented R4 and nothing else we need. Covalent is
  now DataRobot-owned following the Agnostiq acquisition, which adds single-vendor risk on top.
- **Apache Beam** is the closest conceptual ancestor: a map/reduce/shuffle expression whose
  runner is chosen at submit time, descended from Google's MapReduce and FlumeJava. Its code is
  for streams of cheap records on Flink/Spark/Dataflow, and its naming (`ParDo`, `PCollection`) is
  its own rather than MapReduce's.
- **Temporal** and **DBOS** are not topology libraries. They solve R7 and nothing else here. DBOS
  embeds as a library over Postgres or SQLite with no separate orchestrator, which makes it the
  one to revisit if resumability becomes a hard requirement.

## The measurement

`executor.py` is **371 lines** with **7 abstract methods**. The scheduling — `execute()` walking
`for stage in self.pipeline.stages` and `run_stage()` walking `for step in stage.steps` — is
**32 lines**, including the agent/orchestrator dispatch. Everything else is host provisioning,
launch parallelism, polling, timeouts, archive extraction, branch fetching, gating and manifest
writing.

So a workflow library would replace roughly 32 lines of ours, add between 12 and 127 packages to
a wheel that end users install, and leave the remaining ~340 lines to be written regardless,
because no library models R2.

`graphlib.TopologicalSorter` has been in the standard library since Python 3.9. It takes a
dependency mapping, raises `CycleError` on `prepare()`, and drives parallel execution through
`get_ready()` / `done()` / `is_active()` — the loop an arbitrary-topology executor needs, at no
dependency cost. It is the one piece of the graph problem worth taking from elsewhere.

## Gaps in the current model

None of these is solved by adopting a library. All four are modeling decisions, and they are the
content of the design that follows.

1. **`Pipeline.stages` is a linear tuple, not a DAG.** `execute()` is a `for` loop over stages in
   declaration order. Artifacts only *validate* that ordering —
   `_validate_step_bases_exist_before_the_step_runs` walks stages in declaration order accumulating
   available names — they do not *determine* it. Two independent map stages cannot be expressed as
   concurrent.

2. **`Step.base` is a single `ArtifactName`.** A reducer consuming N mappers' outputs cannot say
   so in the model. Today that relationship is implicit in `discover_jobs(execution)` reading the
   whole `Execution`. The declared topology therefore does not describe the actual dataflow, so the
   SVG, the validation, and any static reasoning a plugin author does all work from an incomplete
   picture.

3. **`Stage.fanout` is prose.** It is a `NonEmptyStr` holding text like
   `'one agent per feature file'`. Fan-out is the model's central operator and needs to be typed,
   not described.

4. **There is no shuffle.** Regrouping N upstream jobs' outputs by key into M downstream jobs is a
   distinct primitive from reducing to one, and nothing in the model expresses it. The MapReduce
   paper already names the pieces: the *shuffle* is the regroup, and the *partition function* is
   what decides which downstream job a given upstream result lands in.

A fifth gap, lower priority: nothing reads `execution.json` back, so an interrupted run is a lost
run (R7). For a topology where one stage costs hours of agent time that is expensive, and it is the
only gap a library would close.

## Recommendation

1. Build the arbitrary-topology model in `mngr_mapreduce`. Do not adopt an orchestrator.
2. Use `graphlib.TopologicalSorter` for readiness and cycle detection. Add no dependency.
3. Keep the `Pipeline` / `ExecutionPlan` split exactly as it is. No candidate separates the two as
   cleanly.
4. Do not import a vocabulary. This codebase already speaks MapReduce: the package is
   `mngr_mapreduce`, and `AgentKind` is already `MAPPER` / `REDUCER`. Where the new model needs a
   word it does not yet have, take it from the MapReduce paper: *shuffle* for the regroup between
   nodes, *partition function* for what assigns an upstream result to a downstream job, *combiner*
   for a partial reduce.
5. Treat resumable executions as a separate, later decision. If it becomes a requirement, DBOS is
   the candidate to evaluate, because it embeds as a library rather than demanding a cluster.
