A lead now waits for its background agent's report the same way on every harness. The skills that dispatch a worker (`launch-task`, `crystallize-creation`, `update-creation`, `heal-creation`, `fetch-process-show`, `migrate-workspace`, `publish-template`, `update-self`, and the shared `lead-proxy.md`) start `create_worker.py await` through `system/scripts/run_in_background.py` instead of claude's background tool, and the report arrives in the lead's chat as a message. Before, a codex lead had no way to be woken by the report. It improvised its own notifier, and the chat showed the report as a "Browser fleet" message.

`update-self` copies `run_in_background.py` out of the release it is updating to before it waits, because the workspace being updated may be too old to have the script.

`migrate-workspace`'s two backups and `publish-template`'s wait for the GitHub approval also run through `run_in_background.py` now, so no skill waits on claude's background tool any more.
