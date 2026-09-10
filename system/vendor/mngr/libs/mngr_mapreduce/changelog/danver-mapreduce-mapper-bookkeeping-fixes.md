Fixed three mapper-bookkeeping bugs in the map-reduce framework:

A mapper that published an outputs archive the framework could not read or extract was reported as a success. It now carries the error summary "Mapper published an archive but it could not be extracted." in both the mid-run and final reports, and no longer counts toward the "at least one mapper succeeded" gate that launches the reducer (which would otherwise have run against inputs that were never extracted).

Mappers are now launched with a `mapreduce_task_id` label holding the task id. The `--reintegrate` flow already read that label but nothing ever wrote it, so every reintegrated run keyed its mappers by agent name instead of by task id.

`--reintegrate` no longer treats the run's snapshotter agent as a mapper. It selected every agent of the run whose role was not `REDUCER`, so the snapshotter (which never publishes a mapper outputs archive) showed up in the reintegrated report as a failed mapper reading "Could not pull outputs during reintegrate". Selection is now by the `mapreduce_role` label being `MAPPER`.
