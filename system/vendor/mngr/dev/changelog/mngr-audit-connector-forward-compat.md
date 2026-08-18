Implemented the connector-client forward-compatibility plan (`blueprint/connector-forward-compat/`), added during this branch's earlier planning phase.

Repo-root pieces: `style_guide.md` gains a "Wire models (cross-version HTTP responses)" section (tolerant WireModel/WireEnum bases, UNKNOWN enum semantics, list failure rules, additive-with-defaults server changes proven by golden compat tests, preserve-on-absent for round-tripped fields).

`test_meta_ratchets.py` gains two guards: WireModel subclasses may never re-tighten `extra` to `"forbid"` (mirroring the EventEnvelope guard), and every class in a `wire_types.py` must be a WireModel or WireEnum.

CI hardening: the report-flaky-aware-tests action's final check-run POST now retries transient GitHub API failures with backoff and degrades to a warning instead of failing the job -- a GitHub 503 outage had turned three all-tests-passing jobs red at exactly that call.
