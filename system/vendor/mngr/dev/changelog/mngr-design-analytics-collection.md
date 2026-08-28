Minds analytics phases 3-5 (in-workspace collection) land in `apps/analytics`; the root-level pieces:

- `scripts/delete_accounts.py` now wires the analytics deletion path into its per-account cascade: transcript-lake rows are deleted and a `deletion_events` fact row is written before the SuperTokens identity is removed, skipping (with a warning) on tiers without an analytics bringup. The dry-run "WOULD DELETE" log line states whether the analytics transcript step would run.

- `.minds/template/analytics.sh` gains the transcripts-lake catalog/bucket keys and the optional collection tuning knobs (interval, parallelism, per-workspace timeout, input budget).

- `specs/minds-analytics/`: the spec's collection section is no longer marked future, and the redaction contract's version stamp is defined as the injected file set's sha256 content hash (there is no git in the collection container).

- `test_meta_ratchets.py`: the repo-wide class-definition scan behind the three model-config guards is now cached (one AST walk per process instead of three) and those tests carry the file's standard 60s timeout, fixing a CI-load-dependent timeout of `test_wire_types_files_contain_only_wire_models_and_wire_enums`.
