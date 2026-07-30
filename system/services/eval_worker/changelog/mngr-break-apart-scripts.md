New package: the eval worker moves out of `system/scripts/` (`eval_responder.py`, `eval_sink.py`, `eval_decider.py`, `eval_wait_watcher.py`) into a proper `system/services/eval_worker/` service package with an `eval-worker` console script, taking its `boto3` dependency with it (previously on the workspace root).

The harness-slotted config moves from `system/scripts/test_case_metadata.json` to `system/services/eval_worker/test_case_metadata.json`, and the terminal done marker moves from the stale pre-restructure `runtime/eval_done` to `data/.state/eval/done`.

The supervisord one-shot is renamed from `[program:eval-responder]` to `[program:eval-worker]` and now runs the console script.
