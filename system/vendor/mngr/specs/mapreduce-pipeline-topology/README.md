# Arbitrary-topology pipelines for mngr-mapreduce

Design work for replacing `mngr_mapreduce`'s linear stage model with a pipeline that can express any arrangement of mappers, reducers and shuffles.

Read in this order:

1. [`library-survey.md`](library-survey.md) -- why this is built here rather than on Airflow or any other existing orchestrator. A closed decision; read it for the reasoning, not for what to do next.
2. [`requirements.md`](requirements.md) -- the contract the design must satisfy. **This is the document to argue with.**
3. [`arbitrary_topology_pipelines.md`](arbitrary_topology_pipelines.md) -- one design that claims to satisfy the requirements, with a traceability table showing which section answers which requirement.

The requirements document is upstream of the design: if a requirement changes, the design follows.
