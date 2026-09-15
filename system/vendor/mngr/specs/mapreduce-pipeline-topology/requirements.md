# Requirements: arbitrary-topology pipelines for mngr-mapreduce

## Purpose

This is the requirements document for replacing `mngr_mapreduce`'s linear stage model with an arbitrary-topology pipeline model.
It is the contract the design must satisfy; [`arbitrary_topology_pipelines.md`](arbitrary_topology_pipelines.md) is one design that claims to satisfy it, and [`library-survey.md`](library-survey.md) is the record of why the layer is built here rather than on an existing orchestrator.

This document is the thing to argue with.
If a requirement below is wrong, too strong, too weak, or missing, change it here and the design follows.

## How to read this

**MUST** is a hard requirement; a design that does not meet it is rejected.
**SHOULD** is a strong preference; a design that does not meet it needs a stated reason.
**MAY** is permission, not obligation.

IDs are stable and grouped by concern, so a new requirement can be inserted without renumbering: `M` for what the model can express, `E` for how an execution behaves, `C` for external constraints, `N` for what is deliberately out of scope.
Each requirement is one statement followed by its rationale; the rationale is context, not part of the contract.

## Definitions

**Job.**
One agent's assignment within a node.
A node's fan-out produces zero or more jobs, and each job becomes one launched agent.

**Operator.**
Whoever runs the executor.
The operator checkout is the source repo agents are created from and published branches are fetched into.

**Succeeded.**
A job has succeeded when it published an outputs archive, recorded no error, and passed every gate its node declares.
E3, E7 and E11 are all written in terms of job success, so the word needs a fixed meaning; this definition describes what the executor computes rather than mandating how a node must decide, which is the part E8 declined to settle at this level.
A node succeeds on a different test -- the fraction of its jobs that did, under E7.

## M -- what the model can express

**M1. A pipeline MUST be constructible as data and fully validated before any agent launches.**
Plugin authors build `Pipeline` objects; nothing may require the topology to be recovered by tracing execution or by parsing source.
This is what makes pre-flight validation, diagram rendering, and the pipeline/plan split possible.

**M2. A pipeline MUST express any directed acyclic graph of nodes.**
Arbitrary numbers of mappers and reducers in any arrangement, not one fan-out followed by one fan-in.

**M3. A node MUST be able to depend on more than one upstream product.**
Today `Step.base` is a single artifact name, so a reducer consuming many mappers' outputs cannot say so and the relationship hides inside `discover_jobs`.

**M4. What a node waits for MUST be separate from the git ref its agent starts from.**
A reducer waits on a hundred mappers but starts from the base commit, not from any one mapper's branch.
Conflating the two is what forced M3's dependency into runtime code.

**M5. Fan-out shape MUST be machine-readable.**
Today `Stage.fanout` is prose, so the central operator of the model is invisible to validation, to diagrams, and to any shared machinery.
Fan-out *width* is explicitly not required to be static; see E1.

**M6. The model MUST express a shuffle.**
Regrouping many upstream results by key into a different number of downstream jobs is a distinct primitive from reducing to one, and nothing in the current model expresses it.

**M7. A pipeline MUST be renderable to a diagram without being executed.**
The diagram is how a reader checks that the topology they wrote is the topology they meant.

**M8. A structurally invalid pipeline MUST be unconstructible.**
Cycles, dangling dependencies, and ambiguous fan-in are caught when the object is built, not when an agent is already running.

**M9. A pipeline SHOULD be readable by someone who did not write it.**
Node names, artifact names and summaries are the documentation of what a pipeline does; they are not decoration.

**M10. The prompt template for each node is a component of a Pipeline.**
Nodes instantiate agents which are handed prompts to execute. The prompt template is part of the specification of a Pipeline.
Pipelines can be parameterized with context, which is then available for interpolation in the templates of nodes.

## E -- how an execution behaves

**E1. Fan-out width MUST be decided at runtime from what upstream produced.**
How many fixers run depends on how many tests failed, which is not knowable when the pipeline is written.

**E2. Independent nodes MUST be concurrent in structure, and MAY be limited in parallelism.**
Two nodes that do not depend on each other are both eligible the moment their dependencies are met; nothing about the topology serializes them.
How many run at once is a cap in the execution plan, not a property of the pipeline -- concurrency is not parallelism.
`ExecutionPlan.max_concurrent_nodes` defaults to 0, meaning unlimited, and operators tune it down when their machine or their provider quota requires it.

**E3. A node that runs jobs and does not meet its completion requirement ends the whole run.**
An intermediate error state invalidates the correctness of everything downstream, so the run is terminated rather than allowed to produce a partially inconsistent result.
A node that runs *no* jobs is a different case and is not an error; see E11.

**E4. Placement MUST be chosen per node at runtime, against a pipeline held constant.**
The same pipeline object runs one way on a laptop and another way in CI but are expected to produce the same result.
Pipelines are not expected to depend on their means of execution.

**E5. The same pipeline MUST run unmodified on every provider mngr supports.**
Local, Docker and Modal at minimum.
This is the property that makes the layer worth building for other engineers rather than for one plugin.

**E6. An execution MUST be able to bound the total number of agents running across all nodes.**
Per-node caps were sufficient when nodes ran one at a time; with E2, several nodes' caps multiply.

**E7. A node MUST meet a declared completion fraction, or the run fails.**
`required_completion` is a float on the node: the fraction of its jobs that must succeed for the node to succeed.
It defaults to `1.0`, so every job must succeed unless the author says otherwise; a partially inconsistent state is worse than a fully failed but consistent one.
Every reason for failure counts the same for now -- timeout, gate failure, launch failure, a missing archive.

*Future work, highly likely:* those reasons will need to be categorised and weighted separately.
"The agent timed out" and "the agent produced work that failed a gate" are not the same event, and collapsing them into one fraction is a first approximation, not the end state.

**E8. A job MUST be accepted or rejected by mechanical gates and not by the agent's own claim.** **Rejected**
I am hereby rejecting:
> An agent reporting success is evidence, not proof.
I agree with this in principle, but this is solved at the wrong lever of abstraction.
Let the requirements be silent on this matter, but we shall mandate this in the idiom of Pipelines later.


**E9. Every change to an execution's state SHOULD be observable while it runs.**
> Long runs are watched, not awaited.
Good in principle, but let us evolve this as we go.

**E10. A published outputs archive MUST be collected whenever the executor next looks, even if the deadline has passed by then.**
A deadline is a statement about how long the executor will wait, not a claim about real time, and it is only ever evaluated at the moment the executor looks.
An agent that published at T=100s under a 120s deadline is still sitting on its host when a busy poll pass reaches it at T=130s; nothing has gone away, and the only question is which check runs first.
Blaming the producer for being late when it is the reader that was slow would be a bug dressed as a policy: if the output is there when we look, no harm, no foul.

The converse holds and is the actual purpose of a deadline: a node past its deadline that has *not* published is abandoned, its agent stopped, and its job recorded as failed.
Under E7 the cost of confusing the two cases is the whole run rather than one job, which is why this is a requirement rather than an implementation note.

**E11. A fan-out of zero succeeds vacuously.**
A node whose fan-out produced no jobs has nothing to fail at, and succeeds having produced nothing, the way `all([])` is true.
Emptiness then propagates on its own: each downstream node fans out over nothing, succeeds vacuously in turn, and the run ends green having correctly done nothing.
A node for which finding nothing *is* a mistake says so with `min_job_count`, which a discovery node sets to `1` to assert that an empty corpus is an error rather than a quiet success.

**E12. The scheduler MUST handle at least 520 agents running at once within a single node.**
The widest run today is `mngr tmr` over `libs/mngr`'s release suite at 346 agents, one per test, with no parallelism cap set in CI.
520 is that number plus the 50% margin.

## C -- external constraints

**C1. The change SHOULD NOT add a new runtime dependency without justification.**
> `mngr` ships as a wheel end users `pip install`, and `libs/mngr_mapreduce` is inside it.

I'm softening this language. We have every right to add new external dependencies, but should be judicious about it.

**C2. The existing `MapReduceRecipe` path MUST keep working unchanged.**
`mngr tmr` runs on `cli.py` and `orchestration.py` today, and this change must not touch it.

**C3. The implementation MUST work on macOS and Linux.**

**C4. The executor MUST NOT assume local filesystem access or same-machine process management for agent hosts.**
Hosts are frequently remote; host interactions go through `HostInterface` / `OnlineHostInterface`.

**C5. Agent, branch and label naming MUST stay discoverable by `mngr ls`.**
Existing filter expressions over the `mapreduce_role` and `mapreduce_run_name` labels must keep matching.

**C6. Remote operations SHOULD be batched.**
Every host call is a network round trip, and a pipeline multiplies them by its node count.

## N -- out of scope for this version

**N1. Resumability.**
The execution manifest is written but never read back; an interrupted run stays a lost run.
Deferred deliberately; the survey records DBOS as the candidate to evaluate if this is picked up.

**N2. Retries and speculative re-execution.**
The MapReduce paper's answer to stragglers is backup tasks, which fits this model well and is a natural follow-on, but no node is retried in this version.

**N3. Cross-execution caching.**
Every execution starts from its base commit and re-runs every node.

## Open questions

Resolved questions are kept with their answers, because the answer is often the reason a requirement reads the way it does.

**1. Should a node declare a minimum success threshold?** *(Resolved -- E7.)*
> Not right now. Nodes acceptance is binary: all required by default, partial_ok as a specific opt-in

Superseded by the later decision to make this a `required_completion` float rather than a boolean, which is what E7 now says.

**2. Should orchestrator nodes run concurrently with agent polling, given that they mutate the operator checkout and concurrent git is unsafe there?** *(Resolved.)*
> I don't know what this means, but this sounds scary so probably not.

Restated: an orchestrator node does its work in the executor's own process, typically git operations in the operator checkout, while agents elsewhere are mid-flight.
While it runs, either the executor stops polling those agents, or it polls them from another thread and two things touch the same checkout at once.
E2 keeps this live, because independent nodes are concurrent by structure.

*Resolved, and refined in implementation:* an orchestrator node runs inline on the executor's single-threaded loop, so it has the operator checkout to itself without a lock existing at all.
Polling pauses for its duration rather than continuing around it, which E10 makes harmless -- an agent that publishes meanwhile is collected when the loop next reaches it, however long that took.
The lock this answer originally proposed would have bought continued polling at the price of two threads on one git checkout, which is the hazard it was meant to prevent.

**3. Does anything need to depend on a *specific* artifact of a multi-artifact node?** *(Resolved.)*
> Node level granularity is our design at the moment.

**4. How wide does a pipeline run at its widest?** *(Resolved -- E12.)*
> How wide does TMR runs in CI go today? That + 50% saftery factor should be our number.

Measured: `mngr tmr` over `libs/mngr`'s release suite collects 346 tests and runs one agent per test, with no parallelism cap set in CI.
`tmr-minds` is 6 and `tmr-behaviors-minds` is 12 feature files.
346 plus 50% is 520, which is E12.

**5. What should `required_completion` default to?** *(Resolved -- stays `1.0`.)*
E7 sets it to `1.0`, which preserves the strict intent, and it stays there: an author running a wide node is the one who should decide what partial harvest they can live with, and a default that quietly scales would decide it for them.
The arithmetic below is why a wide node will nearly always set it lower.
For a 346-job node, treating each job as independent with success probability *p*, a three-sigma-safe completion fraction is roughly:

| Per-agent success | Safe `required_completion` |
|---|---|
| 99% | 0.97 |
| 95% | 0.91 |
| 90% | 0.85 |

So `1.0` on a wide node means any single agent failure ends the run.

**6. What does it mean for a job to succeed, now that E8 is rejected?** *(Resolved -- see Definitions.)*
The requirements define the term and decline to mandate the mechanism, which is the distinction E8 was rejected for missing.
