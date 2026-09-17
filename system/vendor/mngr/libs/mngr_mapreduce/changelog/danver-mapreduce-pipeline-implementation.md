Replaced the linear stage pipeline model with an arbitrary-topology one. A `Pipeline` is now a set of `Node`s carrying no order: each declares the artifacts it `needs`, and execution order is derived from that, so a pipeline can express any directed acyclic graph of mappers, reducers and shuffles rather than one fan-out followed by one fan-in. `Stage` and `Step` are gone, collapsed into the single `Node` type.

Fan-out is now machine-readable rather than prose. A node declares one of `SINGLE`, `PER_UPSTREAM_JOB`, `PER_PARTITION` (the shuffle) or `DISCOVERED`, and the framework does the fan-out arithmetic -- gathering the upstream successful results, grouping them by partition key -- so a plugin supplies only the content of one job. Filtering needs no primitive: a `PER_UPSTREAM_JOB` binding returns `None` for results that need no work, and a node that fans out to nothing succeeds vacuously, propagating emptiness to a green finish. `min_job_count` is how a node says that finding nothing is instead a mistake.

Prompts are part of the pipeline. A node carries the Jinja template its jobs' prompts render from and declares the variables its binding supplies per job; the executor renders the prompt, and a pipeline whose template reads a name nothing supplies is refused at construction.

A node succeeds when it meets `required_completion`, the fraction of its jobs that must succeed, defaulting to `1.0`; a node that falls short halts the whole run. Concurrency is structural and parallelism is a cap: every node whose dependencies are met is eligible, and the new `ExecutionPlan.max_concurrent_nodes` and execution-wide `max_running_agents` decide how many nodes and agents actually run at once. Scheduling uses `graphlib.TopologicalSorter` from the standard library, so no orchestration dependency was added.

`pipeline_svg.py` now lays a pipeline out rank by rank, so nodes that can run together share a row.

The older `MapReduceRecipe` flow that `mngr tmr` runs on is unchanged.

`MngrPipelineExecutor` runs these pipelines on real mngr agents, placing a node's agents on the operator's own host, on a per-node snapshot and host pool, or on a fresh host per agent, whichever the node's provider supports.

Who performs a node is the node's work rather than a flag beside it. `Node.work` is a discriminated union of `AgentWork` and `OrchestratorWork`, so an orchestrator node has no prompt template, fan-out, gate or completion threshold to set wrongly, and an agent node cannot carry an empty template. `NodeOutcome.produced` carries the same union, which resolves an ambiguity the flat form could not express: an empty `job_results` used to mean either an orchestrator node or an agent node whose fan-out was empty, and those are the two cases the vacuous-success rule depends on telling apart.

A job that met its bar is now described as having **succeeded** rather than having been **accepted**. Gates still accept and reject; what they decide is whether the job succeeded. `JobResult.is_accepted` becomes `is_successful`, and `Execution.accepted_job_results_of` becomes `successful_job_results_of`.

Six fixes from review. Exceptions from a binding, a provider or a gate now fail their node rather than the process, and `execute()` halts on the way out of any exception at all, so nothing leaves it with agents still running. A gate is told whether the job it is judging produced a branch, since an agent that publishes without committing is a legitimate no-op. A branch bundle that fails to apply is now distinguished from an agent that committed nothing, and only the first fails the job. `Parameter.default` is applied and a parameter with no default is refused before any host is provisioned. An agent that is created but then fails to be prompted is stopped rather than left running. A node the halt catches mid-flight keeps the job results already pulled from it.

`NodeName` is now a `SafeName`, because it becomes a segment of every agent and branch name the node produces.

A fan-out that runs over an upstream node now names the artifact it fans out over, so a node is free to need other things as well: a reviewer fans out over the mappers' branches and still reads the guide a setup node wrote. Previously every producer-backed need had to come from the fan-out source, which pushed the second dependency back into binding code.

A pipeline carries its own template family. `Pipeline.template_by_name` holds the shared Jinja sources a node's prompt template may `extends` or `include`, so a template family composes without a loader reaching outside the pipeline object, and the construction-time check sees through inheritance: a template that extends a name the pipeline lacks, or that reads a variable nothing supplies through any template in the family, is refused when the pipeline is built.

Every agent is created without a message and prompted afterwards. Handing the prompt to agent creation meant a delivery failure raised before there was an agent handle to record, so the agent was left running and unreachable by the halt path: the first real pipeline run leaked two idle sessions on the local provider when mngr timed out waiting for message submission evidence, and on a remote provider it would have leaked their hosts.
