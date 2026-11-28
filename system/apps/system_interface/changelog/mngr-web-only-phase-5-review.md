`GET /api/workspace/update` answers what the workspace's update is doing, worked out by the workspace when it is asked, in place of the files a client used to work that out from.

- The answer carries `activity`, `run`, `start_failure`, `schedule`, `needs_sign_in`, `is_update_skipped`, `will_self_arm`, `is_backup_configured`, `allowed_actions` and `as_of`, each typed, with closed sets for the activity, the verdict, who started the run, why a start failed, why a window was skipped, and the actions. It also answers `installed_version`, beside `settings` and `channel_version`, which a client compares to tell whether an update is on offer.

- The files are no longer answered: `schedule` (now the derived schedule), `last_dispatch`, `run_chat_id`, `is_signed_in`, `skipped_target` and `dismissals` are gone, and `?check_sign_in=1` with them. The run's own record is answered as `run_record`, and `last_start` stays, for the Imbue Studio desktop's backend.

- Every write of the update routes answers that state under `state`, as the write left it: the start, the arm, the refresh, the cancel, the skip, the dismissals and the settings write. The start and the arm no longer answer `chat_agent_name`, `chat_id`, `run_started_at` or `schedule`, the skip no longer answers `skipped_target`, and the settings write answers the settings as the state's `settings`.

- `POST /api/workspace/update/dismissals` also takes `start_failure_attempt_id`, which puts a failed start away for every client.

- `GET /api/workspace/update/liveness` and its operation `update.liveness.read` are removed: the update read answers what the route was read for.

`workspace_routes_api.json` describes the new shapes.
