# Arbitrary-topology pipelines for mngr-mapreduce

## Purpose and audience

This spec replaces `mngr_mapreduce`'s linear stage model with a directed acyclic graph of nodes, so that a pipeline can express any arrangement of mappers, reducers and shuffles rather than one fan-out followed by one fan-in.
It is written for whoever implements the change, and for the Imbue engineers who will build `mngr` plugins on top of the result.

[`requirements.md`](requirements.md) is the contract; this document is one design that claims to satisfy it.
If a requirement there changes, this document follows, not the other way around.
[`library-survey.md`](library-survey.md) records why the layer is built here rather than on an existing orchestrator.

**Vocabulary note.**
This package already speaks MapReduce.
Where a new word is needed, it comes from the MapReduce paper: *shuffle* for a regroup between nodes, *partition function* for what assigns an upstream result to a downstream job, *combiner* for a partial reduce.

## Background: what exists today

The model on `main` (`libs/mngr_mapreduce/pipeline.py`) describes a pipeline as validated data, and `pipeline_svg.py` renders it without executing anything.
The unmerged branch `danver/mapreduce-pipeline-executor` adds an executor and an `ExecutionPlan` that separates *where* a run happens from *what* it is.
This spec keeps both separations.

Five properties of the current model block the goal.

1. **`Pipeline.stages` is a linear tuple.**
   `execute()` is a `for` loop over stages in declaration order.
   Artifacts only *validate* that order; `_validate_step_bases_exist_before_the_step_runs` walks stages as declared and checks each step's base is already available.
   Two independent map nodes cannot be expressed as concurrent.

2. **`Step.base` is a single `ArtifactName`.**
   A reducer consuming many mappers' outputs cannot say so.
   Today that relationship is implicit in `discover_jobs(execution)` reaching into the whole `Execution`, so the declared topology does not describe the actual dataflow.

3. **`Stage.fanout` is prose.**
   It is a `NonEmptyStr` holding text like `'one agent per feature file'`.
   Fan-out, the model's central operator, is not represented.

4. **There is no shuffle.**
   Regrouping many upstream results by key into a different number of downstream jobs is a distinct primitive from reducing to one, and nothing expresses it.

5. **Prompts are not part of the pipeline.**
   `build_mapper_prompt` composes a string at runtime, so the pipeline object does not describe what its agents are asked to do.
   `mngr tmr` already keeps its prompts as Jinja templates on disk (`apps/minds/tmr/mapper.j2`, loaded by `libs/mngr_tmr/imbue/mngr_tmr/prompts.py`), which this model formalises.

## Requirements

Traceability, so an unaddressed requirement is visible.

| Requirement | Answered by |
|---|---|
| M1 pipeline is data, validated up front | [The pipeline graph](#the-pipeline-graph), [Validation](#validation) |
| M2 any directed acyclic graph | [The pipeline graph](#the-pipeline-graph) |
| M3 more than one upstream product | `Node.needs` |
| M4 waiting is separate from the starting ref | `Node.needs` versus `Job.base_ref` |
| M5 fan-out shape is machine-readable | [Fan-out shape](#fan-out-shape) |
| M6 shuffle | `FanoutKind.PER_PARTITION`, [Bindings](#bindings) |
| M7 renderable without executing | [Diagrams](#diagrams) |
| M8 invalid pipelines are unconstructible | [Validation](#validation) |
| M9 readable by a stranger | [Worked example](#worked-example) |
| M10 prompts are part of the pipeline | [Prompts and context](#prompts-and-context) |
| E1 runtime fan-out width | [Bindings](#bindings) |
| E2 concurrent in structure, capped in parallelism | [The scheduler](#the-scheduler) |
| E3 a node that falls short ends the run | [Node outcomes](#node-outcomes-and-halting) |
| E4 per-node placement at runtime | [Execution plan](#execution-plan) |
| E5 same pipeline on every provider | [Execution plan](#execution-plan) |
| E6 execution-wide agent ceiling | [Execution plan](#execution-plan) |
| E7 declared completion fraction | [Node outcomes](#node-outcomes-and-halting) |
| E9 state is observable while running | `ExecutionObserverInterface`, [Bindings](#bindings) |
| E10 published beats deadline | [The scheduler](#the-scheduler) |
| E11 a fan-out of zero succeeds vacuously | [Node outcomes](#node-outcomes-and-halting) |
| E12 520 agents in one node | [The scheduler](#the-scheduler) |
| C1 dependencies are justified | [Prompts and context](#prompts-and-context) |
| C2 recipe path untouched | [Migration](#migration) |
| C5 naming stays discoverable | [Naming](#naming) |
| N1-N3 deferred | [Non-goals](#non-goals) |

E8 is rejected at requirements level; gates remain in this design and the [Definitions](requirements.md#definitions) section fixes what it means for a job to succeed.
`C3`, `C4` and `C6` are properties of the implementation rather than of this design, and are checked at review time.

## Design

### The pipeline graph

A pipeline becomes a set of nodes with no declared order.
Order is derived from the artifacts each node needs.

```python
class AgentWork(FrozenModel):
    """What an agent node asks of its agents: how it fans out, what it tells them, and what counts as success."""

    kind: Literal[NodeKind.AGENT] = NodeKind.AGENT
    fanout: Fanout = Field(description="How the node decides how many jobs to run")
    prompt_template: NonEmptyStr = Field(description="Jinja source rendered into each job's prompt")
    job_variables: tuple[NonEmptyStr, ...] = Field(
        description="The template variable names the node's binding supplies per job, beyond the pipeline's parameters"
    )
    gates: tuple[Gate, ...] = Field(description="The mechanical checks run on each job the node produced")
    required_completion: UnitFloat = Field(
        default=UnitFloat(1.0),
        description="The fraction of the node's jobs that must succeed for the node to succeed",
    )
    min_job_count: NonNegativeInt = Field(
        default=NonNegativeInt(0),
        description="How many jobs the node must find at all; 0 means finding none is a legitimate no-op",
    )


class OrchestratorWork(FrozenModel):
    """What a node does in the executor's own process. It launches no agent, so it has no prompt, no fan-out and no gates."""

    kind: Literal[NodeKind.ORCHESTRATOR] = NodeKind.ORCHESTRATOR


class Node(FrozenModel):
    """One unit of work in a pipeline: what it waits for, what it must deliver, and what it does."""

    name: NodeName = Field(description="Unique within the pipeline; the node segment of its agent and branch names")
    summary: str = Field(description="What the node does, in a sentence or two")
    needs: tuple[ArtifactName, ...] = Field(
        description="The artifacts that must exist before the node runs; empty for a node that waits for nothing"
    )
    produces: tuple[Artifact, ...] = Field(description="The artifacts the node must deliver when it finishes")
    work: AgentWork | OrchestratorWork = Field(discriminator="kind", description="Who performs the node, and everything that depends on the answer")


class Pipeline(FrozenModel):
    """The whole pipeline as data: what the operator supplies, the nodes, and what the run delivers."""

    name: str = Field(description="The pipeline's name")
    parameters: tuple[Parameter, ...] = Field(description="The context an execution supplies, available to every prompt template")
    inputs: tuple[Artifact, ...] = Field(description="The artifacts the operator supplies before the run starts")
    nodes: tuple[Node, ...] = Field(description="The nodes, in no particular order; execution order is derived from needs")
    outputs: tuple[Artifact, ...] = Field(description="The artifacts the run delivers to the operator")
```

**Who performs a node is the node's work, not a flag beside it.**
A prompt template, a fan-out, gates and the two thresholds all mean something for an agent node and nothing for an orchestrator node.
Carrying them beside an `actor` flag would make every mismatched combination constructible and leave a validator to reject them, with an empty string standing in for "this node launches no agent".
As a discriminated union there is no validator to write: an orchestrator node has no such field to set, and `prompt_template: NonEmptyStr` carries the non-emptiness an agent node needs.
The tag lives inside the variant that owns the fields, so it cannot disagree with them.

`UnitFloat` is a new primitive in `imbue_common`: a float in the closed unit interval, mirroring the existing `Probability` but without claiming to be one, since `required_completion` is a required fraction rather than a chance.

`needs` replaces `Step.base`.
`base` conflated what a node waits for with the git ref its agent checks out.
A reducer waits on a hundred mappers but starts from the base commit, not from any one mapper's branch, which is why that conflation forced the dependency into `discover_jobs`.
`needs` is topology and lives in the model; the ref a job starts from is per-job data and stays on `Job.base_ref`, decided by the binding.

```python
class Job(FrozenModel):
    """One agent's assignment within a node: where it starts, what it is handed, and what its prompt is rendered from."""

    slug: str = Field(description="Distinguishes the job within its node; the last segment of the agent and branch names")
    base_ref: str = Field(description="Commit or branch in the source repo the agent starts from")
    template_variables: dict[str, str] = Field(description="Values for the node's job_variables, merged over the execution's context")
    inputs_dir: Path | None = Field(description="Local directory copied into the agent's work dir before the prompt is sent, or None")
```

This is today's `StageJob` with one change: a binding no longer composes a prompt string, it supplies the values the node's template renders from.
The prompt is a property of the pipeline; the binding contributes only what varies per job.

Edges are derived, not declared.
Artifact names are unique across the whole pipeline and each is produced by exactly one node, so `artifact name -> producing node` is a function.
Node `N` depends on node `M` when any of `N.needs` is produced by `M`.
A name in `needs` that is a pipeline input contributes no edge, because the operator supplied it before the run.

Dependency granularity is node-level, per the resolution of requirements question 3.
`needs` names artifacts rather than nodes because a pipeline input has no producing node, and because the artifact is the contract a node promises to deliver.
For scheduling alone, node names would do just as well; artifact names are what make the promise legible and what the diagram labels its edges with.

`Stage` and `Step` both disappear.
A stage was three things at once -- a unit of fan-out, a scope for gates, and a scope for placement -- and a stage holding several steps was a chain of nodes written as a list.
As one node type, what were two steps in different stages can run concurrently, which the nested form could not express.

### Fan-out shape

```python
class FanoutKind(UpperCaseStrEnum):
    """How a node decides how many jobs to run."""

    SINGLE = auto()
    PER_UPSTREAM_JOB = auto()
    PER_PARTITION = auto()
    DISCOVERED = auto()


class Fanout(FrozenModel):
    """How a node fans out, as both a machine-readable kind and the prose a diagram shows."""

    kind: FanoutKind = Field(description="Which fan-out the framework performs for the node")
    description: str = Field(description="How the fan-out reads to a person, e.g. 'one agent per feature file'")
```

| Kind | Job count | The MapReduce analogue |
|---|---|---|
| `SINGLE` | exactly one | the reducer |
| `PER_UPSTREAM_JOB` | one per successful upstream job, minus those the binding declines | a map over the previous node's results |
| `PER_PARTITION` | one per distinct partition key | the shuffle, with a partition function |
| `DISCOVERED` | decided by the binding | the initial map over an input split the pipeline cannot see |

The framework owns the arithmetic and the binding owns the content.
Without a typed kind, every plugin re-implements "gather the successful upstream results" and "group them by key".

**Filtering is fan-out.**
There is no filter primitive, because a filter is a fan-out that produces fewer jobs than it was offered.
A `PER_UPSTREAM_JOB` binding returns `None` for results that need no work; a `DISCOVERED` binding returns a shorter list.
When it returns nothing at all, [E11](#node-outcomes-and-halting) applies and the node succeeds vacuously.

### Prompts and context

A pipeline declares the parameters an execution must supply; each node carries the template its jobs' prompts are rendered from.

```python
class Parameter(FrozenModel):
    """One value an execution supplies, available to every node's prompt template."""

    name: NonEmptyStr = Field(description="The name the templates interpolate")
    description: str = Field(description="What the value is and where it comes from")
    default: str | None = Field(default=None, description="Used when the execution does not supply the parameter")
```

The prompt for one job is `node.prompt_template` rendered against `{**execution.context, **job.template_variables}`.
The execution's context is fixed for the whole run; the job's variables are what the binding varies per agent.
Rendering happens in the executor, immediately before the prompt is delivered, so a `Job` never carries a composed string and the pipeline remains the single description of what agents are asked.

Jinja is the template engine.
It is [already a dependency](../../libs/mngr_tmr/pyproject.toml) of `mngr_tmr` and `mngr_forward`, and `mngr tmr` already ships `.j2` mapper prompts, so C1 is satisfied without adding anything.

Templates are checked before any agent launches, which extends M8 to prompts.
`jinja2.meta.find_undeclared_variables` yields every variable a template reads; each must be a pipeline parameter or one of the node's declared `job_variables`, or the pipeline is refused.
At runtime the executor asserts the binding actually supplied every name in `job_variables`, because the type system cannot guarantee a `dict[str, str]` has particular keys.

### Bindings

Behavior still attaches to names, and a pipeline with an unbound node or gate is still refused before any agent launches.
The single `discover_jobs` hook splits into one interface per fan-out kind, so each binding receives exactly what its shape implies and returns exactly what its shape allows.

```python
class SingleJobBindingInterface(MutableModel, ABC):
    @abstractmethod
    def build_job(self, execution: Execution, node: Node) -> Job:
        """Build the node's one job."""


class PerUpstreamJobBindingInterface(MutableModel, ABC):
    @abstractmethod
    def build_job(self, execution: Execution, node: Node, upstream_result: JobResult) -> Job | None:
        """Build the job for one successful upstream result, or None to decline that result."""


class PerPartitionBindingInterface(MutableModel, ABC):
    @abstractmethod
    def partition(self, execution: Execution, node: Node, upstream_result: JobResult) -> PartitionKey:
        """The partition an upstream result belongs to; one job runs per distinct key."""

    @abstractmethod
    def build_job(
        self, execution: Execution, node: Node, key: PartitionKey, upstream_results: tuple[JobResult, ...]
    ) -> Job:
        """Build the job for one partition."""


class DiscoveredJobsBindingInterface(MutableModel, ABC):
    @abstractmethod
    def discover_jobs(self, execution: Execution, node: Node) -> list[Job]:
        """Return one job per agent to launch; an empty list means the node has nothing to do."""
```

`OrchestratorNodeBindingInterface`, `GateBindingInterface` and `ExecutionObserverInterface` carry over unchanged apart from the stage-to-node rename.

**Upstream is unambiguous by construction.**
`PER_UPSTREAM_JOB` and `PER_PARTITION` require that every artifact in the node's `needs` be produced by the same single upstream node, enforced by a validator.
Without that rule, a node needing artifacts from two producers would fan out over an undefined union of their results.
A node that has to join two upstreams uses `DISCOVERED` and reads the `Execution` itself.

### Validation

Constructing a `Pipeline` raises `PipelineInvariantError` unless all of the following hold.

- Node names are unique, artifact names are unique across inputs and every node's `produces`, gate names are unique within a node, output names are unique, and parameter names are unique.
- Every output either matches the artifact of that name defined in the pipeline, or is defined only in `outputs`.
- Every name in every `needs` is a pipeline input or some node's product.
- The derived graph is acyclic, checked by building a `TopologicalSorter` and calling `prepare()`, which raises `CycleError`.
- A node whose fan-out is `PER_UPSTREAM_JOB` or `PER_PARTITION` has at least one `needs` entry, and all of its `needs` come from one producing node.
- Every variable an agent node's template reads is a pipeline parameter or one of that node's `job_variables`.
- `min_job_count` is not greater than what the fan-out can produce: a `SINGLE` node's `min_job_count` is at most 1.

`Node.work` removes the need for three further rails: an orchestrator node cannot carry a prompt template, a fan-out or gates, and an agent node cannot carry an empty template.

### The scheduler

`graphlib.TopologicalSorter` supplies readiness and cycle detection; the executor supplies everything else.
Concurrency is structural and parallelism is a cap, per E2: every node whose dependencies are met is eligible, and `ExecutionPlan.max_concurrent_nodes` decides how many actually run.

```python
sorter = TopologicalSorter()
for node in pipeline.nodes:
    sorter.add(node.name, *_predecessor_names(pipeline, node))
sorter.prepare()

while sorter.is_active():
    self._admit(sorter.get_ready())        # queue newly eligible nodes; get_ready yields each exactly once
    self._start_admitted_nodes()           # start up to max_concurrent_nodes: discover jobs, provision hosts
    self._launch_up_to_the_caps()          # top up every running node's queue, under both agent ceilings
    self._poll_running_agents_once()       # one pass over every in-flight agent of every running node
    for node_name in self._settled_node_names():
        outcome = self._finish_node(node_name)   # gate, evaluate, record, release hosts
        if outcome.status is NodeStatus.FAILED:
            return self._halt(f"Node '{node_name}' failed: {outcome.detail}")
        sorter.done(node_name)
    pause(self.execution.plan.poll_interval_seconds)
```

`get_ready()` yields each node exactly once and will not yield it again until `done()` is called for it, so an eligible node that cannot start yet is held in the admitted queue rather than dropped.

Five properties of this loop cannot change without breaking a requirement.

**One poll pass covers every running node.**
Today each step blocks on its own agents, so two independent nodes would serialize even though nothing connects them.
E2 requires a single pass over all in-flight agents instead.

**Launching is a top-up, not a one-shot.**
A node's jobs are queued when it starts and launched over many passes, because `NodePlacement.max_running_agents` and `ExecutionPlan.max_running_agents` both bound how many may exist at once.
A started node whose allowance is momentarily zero launches nothing until agents elsewhere finish.

**Published is checked before the deadline.**
`_poll_once` asks `is_published(agent)` first and only then compares against the agent's deadline.
A deadline is a statement about how long the executor will wait, not a claim about real time: if the archive is there when we look, the work counts however late we looked.
The converse is the deadline's actual purpose -- an agent past its deadline with nothing published is stopped and its job recorded as failed.
Under E7 the cost of confusing those two cases is the whole run, not one job.

**An orchestrator node runs on the loop itself, which makes it exclusive.**
Orchestrator work is git work in the operator's repo, and concurrent git there is unsafe.
Running the binding inline on the executor's single-threaded loop gives it the checkout without a lock, because nothing else is touching the checkout while it works.
The cost is that the loop is not polling meanwhile, which E10 bounds: an agent that publishes during a long orchestrator node is collected whenever the loop next reaches it, rather than recorded as a timeout.
A lock would buy continued polling at the price of two threads on one checkout, which is the hazard it exists to prevent.

**Failure halts the whole run.**
Per E3 there is no partial completion: `_halt` stops every in-flight agent, releases every provisioned host, marks every node that never ran as `SKIPPED`, and records `stopped_reason`.

E12 sets the scale this loop is designed for: at least 520 agents in one node, so a poll pass is a bounded sweep over a list of that size rather than anything that fans out per agent.

### Node outcomes and halting

```python
class NodeStatus(UpperCaseStrEnum):
    """How a node ended."""

    SUCCEEDED = auto()
    FAILED = auto()
    SKIPPED = auto()
```

`SUCCEEDED` means the node met both of its thresholds.
`FAILED` means it ran and fell short, or errored outright.
`SKIPPED` means it never ran, because the execution halted first.

A node is evaluated once every one of its jobs has a result:

```python
@pure
def evaluate_node(node: Node, job_results: Sequence[JobResult]) -> NodeStatus:
    if len(job_results) < node.min_job_count:
        return NodeStatus.FAILED
    if not job_results:
        return NodeStatus.SUCCEEDED          # E11: a fan-out of zero succeeds vacuously
    successful_count = sum(1 for job_result in job_results if job_result.is_successful)
    return (
        NodeStatus.SUCCEEDED
        if successful_count >= math.ceil(node.required_completion * len(job_results))
        else NodeStatus.FAILED
    )
```

The vacuous case is checked after `min_job_count` and before the fraction, which keeps the two thresholds independent.
`required_completion` cannot express "must not be empty", because zero successes out of zero jobs satisfies any fraction -- `all([])` is true.
`min_job_count` is the assertion that finding nothing is a mistake; a discovery node sets it to 1, and a filter node leaves it at 0.

`math.ceil` rather than float division keeps the boundary exact: at the default `required_completion` of `1.0` over 346 jobs, the requirement is 346, not 345.99999.

```python
class AgentNodeProduct(FrozenModel):
    kind: Literal[NodeKind.AGENT] = NodeKind.AGENT
    job_results: tuple[JobResult, ...]


class OrchestratorNodeProduct(FrozenModel):
    kind: Literal[NodeKind.ORCHESTRATOR] = NodeKind.ORCHESTRATOR
    outcome: OrchestratorOutcome


class NodeOutcome(FrozenModel):
    node_name: NodeName
    status: NodeStatus
    detail: str
    produced: AgentNodeProduct | OrchestratorNodeProduct = Field(discriminator="kind")


class Execution(FrozenModel):
    pipeline_name: str
    execution_name: str
    base_commit: str
    context: dict[str, str]
    plan: ExecutionPlan
    node_outcome_by_node_name: dict[NodeName, NodeOutcome]
    stopped_reason: str | None
```

`stopped_reason` survives from the current model, because under E3 a failure is global rather than local, and there is a single point at which the run stopped.

The outcome carries the same union, for the same reason.
With `job_results` and an optional `orchestrator_outcome` side by side, an empty `job_results` means either an orchestrator node or an agent node whose fan-out was empty, and telling them apart requires reading the other field.
E11 depends on distinguishing exactly those two cases.

### Execution plan

`ExecutionPlan` keeps its shape.
`placement_by_stage_name` becomes `placement_by_node_name`, and `StagePlacement` becomes `NodePlacement` with its fields unchanged.
Two fields are added.

`ExecutionPlan.max_concurrent_nodes` bounds how many nodes run at once, where `0` means unlimited.
It is the parallelism cap of E2, and it lives in the plan rather than the pipeline because it is an execution-strategy choice made against a topology held constant.

`ExecutionPlan.max_running_agents` is a ceiling across the whole execution, where `0` means unlimited.
`NodePlacement.max_running_agents` caps one node, which was sufficient when nodes ran one at a time; with concurrent nodes those caps multiply, and the executor launches only up to the smaller remaining allowance.

### Naming

Agent and branch names keep their shape, with the stage segment becoming the node segment: agents are `<pipeline>-<execution>-<node>-<slug>` and branches are `<pipeline>/<execution>/<node>/<slug>`.
The `mapreduce_role` label carries the node name, as it carries the stage name today, so existing `mngr ls` filter expressions keep working (C5).

### Diagrams

`pipeline_svg.py` currently lays out inputs, then each stage in declaration order, then outputs, top to bottom.
A graph needs layered layout: a node's rank is the longest path from any source node, nodes are drawn rank by rank, and edges are drawn between them.
Within a rank, nodes are ordered by their position in `Pipeline.nodes`, which keeps rendering deterministic and keeps the drift-test convention usable.

## Worked example

A pipeline that maps over a corpus, shuffles the results into per-area groups, fixes each group, and merges the lot.

```python
WITNESS_PIPELINE = Pipeline(
    name="witness",
    parameters=(
        Parameter(name=NonEmptyStr("corpus_root"), description="Directory holding the behavior corpus"),
        Parameter(name=NonEmptyStr("style_guide"), description="Path to the style guide agents must follow"),
    ),
    inputs=(Artifact(name=ArtifactName("corpus"), description="The behavior corpus at the base commit"),),
    nodes=(
        Node(
            name=NodeName("generate"),
            summary="Write witness tests for one feature file.",
            needs=(ArtifactName("corpus"),),
            produces=(Artifact(name=ArtifactName("witness_branches"), description="One branch per feature file"),),
            work=AgentWork(
                fanout=Fanout(kind=FanoutKind.DISCOVERED, description="one agent per feature file"),
                prompt_template=NonEmptyStr(
                    "Write witness tests for {{ feature_file }} in {{ corpus_root }}, following {{ style_guide }}."
                ),
                job_variables=(NonEmptyStr("feature_file"),),
                gates=(Gate(name=NonEmptyStr("tests_run"), description="The branch's witness tests execute"),),
                required_completion=UnitFloat(0.9),
                min_job_count=NonNegativeInt(1),
            ),
        ),
        Node(
            name=NodeName("consolidate"),
            summary="Reduce every witness branch of one area into a single branch.",
            needs=(ArtifactName("witness_branches"),),
            produces=(Artifact(name=ArtifactName("area_branches"), description="One reduced branch per area"),),
            work=AgentWork(
                fanout=Fanout(kind=FanoutKind.PER_PARTITION, description="one agent per behavior area"),
                prompt_template=NonEmptyStr("Consolidate the witness branches for area {{ area }}: {{ branches }}."),
                job_variables=(NonEmptyStr("area"), NonEmptyStr("branches")),
                gates=(Gate(name=NonEmptyStr("tests_run"), description="The reduced branch's tests execute"),),
            ),
        ),
        Node(
            name=NodeName("integrate"),
            summary="Merge every area branch into one result branch.",
            needs=(ArtifactName("area_branches"),),
            produces=(Artifact(name=ArtifactName("result_branch"), description="The branch the run delivers"),),
            work=OrchestratorWork(),
        ),
    ),
    outputs=(Artifact(name=ArtifactName("result_branch"), description="The branch the run delivers"),),
)
```

`generate` is `DISCOVERED` because the number of feature files is not visible to the pipeline, sets `min_job_count=1` so an empty corpus is an error rather than a quiet success, and sets `required_completion=0.9` because at corpus scale demanding every agent succeed would make the run fail most of the time.
`consolidate` is `PER_PARTITION`: its partition function maps each witness branch to its behavior area, and one agent runs per area.
`integrate` is `OrchestratorWork()`, which is the whole declaration: there is no prompt, fan-out, gate or threshold to state, because none of them mean anything for work the executor does itself.
Nothing in the pipeline says where any of it runs; that is the execution plan's job, and the same object runs locally, on Docker or on Modal.

## Failure modes and edge cases

**A cycle in the graph.**
Caught at construction by `prepare()` raising `CycleError`, re-raised as `PipelineInvariantError` naming the nodes involved.
No agent has launched.

**A template that reads an undeclared variable.**
Caught at construction, because every variable must be a pipeline parameter or one of the node's `job_variables`.

**A binding that omits a declared job variable.**
Caught at render time and raised, because a `dict[str, str]` cannot be typed to guarantee particular keys.
The node fails, which halts the run, which is correct: a prompt with a missing variable would otherwise reach an agent as a hole.

**A node whose binding discovers no jobs.**
`min_job_count` decides.
At the default of 0 the node succeeds vacuously and emptiness propagates to a green finish; at 1 or more the node fails and halts the run.

**A `PER_UPSTREAM_JOB` binding that declines every result.**
Indistinguishable from discovering no jobs, and treated identically.
This is the filter case, and it needs no special handling.

**A partition function that raises.**
The node fails with the exception in `detail`, because a partitioner that cannot classify a result leaves the fan-out undefined.

**A gate binding that raises.**
The node fails, because a gate that cannot render a verdict leaves every job undecided, and treating undecided as success would let unchecked work through.
This is distinct from a gate returning a failing `GateResult`, which is an ordinary rejection of one job and is counted against `required_completion`.

**An agent that publishes after its deadline but before the executor looks.**
Collected, per E10.

**An orchestrator binding that runs for a long time.**
It blocks the poll loop for its duration, and anything that publishes meanwhile is collected on a later pass rather than lost.
Orchestrator bindings should be fast relative to `poll_interval_seconds`.

**Two jobs whose slugs sanitize to the same agent name.**
Handled as today, by `dedup_name` against the set of suffixes already used, tracked per node rather than per stage.

**A plan naming a node the pipeline lacks.**
Refused by `assert_plan_fits_pipeline` before any agent launches.

## Non-goals

**Resumability (N1).**
`ManifestWriter` keeps writing the whole `Execution` to `execution.json` after every change, and nothing reads it back.
An interrupted run stays a lost run.

**Retries and speculative re-execution (N2).**
The MapReduce paper's answer to stragglers is backup tasks, which fits this model well and is the natural follow-on, but no node is retried in this version.
`required_completion` is what absorbs straggler loss until then.

**Categorised failure reasons.**
E7 counts every failure the same.
Separating "timed out" from "failed a gate" is flagged in the requirements as likely future work.

**The recipe path (C2).**
`MapReduceRecipe`, `cli.py`, `orchestration.py`, `launching.py` and `pulling.py` are not touched, so `mngr tmr` is unaffected.

**Cross-execution caching (N3).**
Every execution starts from its base commit and re-runs every node.

## Migration

`pipeline.py`, `pipeline_svg.py` and `primitives.py` on `main` are rewritten in place; the model has no external callers yet, so there is nothing to deprecate.
The unmerged `danver/mapreduce-pipeline-executor` branch is discarded rather than merged, and its executor, bindings and manifest are rewritten against this model.
The parts of that branch independent of the linear assumption -- archive and bundle handling, the seven-method executor split, `ManifestWriter`, and the host-placement logic in `MngrPipelineExecutor` -- are carried over rather than rewritten.

| Today | Becomes |
|---|---|
| `Stage`, `Step` | `Node` |
| `StageJob` | `Job` |
| `StageJob.prompt` | `Job.template_variables` plus `Node.prompt_template` |
| `StagePlacement` | `NodePlacement` |
| `StageHosts` | `NodeHosts` |
| `StageOutcome`, `StepOutcome` | `NodeOutcome` |
| `OrchestratorStepOutcome` | `OrchestratorOutcome`, inside `OrchestratorNodeProduct` |
| `Step.actor`, the `Actor` enum | `Node.work`, a discriminated union |
| `StepPath`, `make_step_path` | dropped; bindings key on `NodeName` |
| `placement_by_stage_name` | `placement_by_node_name` |
| `stage_outcomes` | `node_outcome_by_node_name` |
| `AgentStepBindingInterface` | one interface per `FanoutKind` |

`StepPath` disappears because a node is addressed by its own name once stages are gone, which removes the `<stage>/<step>` string-building and the class of bug where a binding is registered under a path no step has.

## Test plan

Unit tests in `pipeline_test.py` for every validator under [Validation](#validation), each asserting on the specific `PipelineInvariantError` message.

Unit tests in `executor_test.py`, driven by a mock executor, covering:

- a diamond topology, asserting the two middle nodes are in flight at the same time;
- `max_concurrent_nodes=1`, asserting the same diamond serializes and still completes;
- `evaluate_node` at its boundaries: `required_completion=1.0` over 346 jobs requiring 346, `0.9` requiring 312, zero jobs succeeding, and zero jobs failing under `min_job_count=1`;
- a `PER_PARTITION` node, asserting one job per distinct key and that each job receives exactly the results of its partition;
- a `PER_UPSTREAM_JOB` node whose binding declines every result, asserting the node succeeds and emptiness propagates to a green run;
- a failing node, asserting every in-flight agent of a concurrent node is stopped, every unreached node is `SKIPPED`, and `stopped_reason` is set;
- the execution-wide `max_running_agents` ceiling, asserting concurrent nodes together never exceed it;
- an agent that publishes during a long orchestrator node, asserting it is collected rather than recorded as a timeout;
- prompt rendering, asserting the prompt an agent receives is the node's template rendered against context merged with job variables, and that a missing declared variable raises.

Rendering tests in `pipeline_svg_test.py`, asserting a diamond lays out in three ranks and that the middle rank's two nodes share a rank.
