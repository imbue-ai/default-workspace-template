New package: the recurring-job machinery moves out of `system/scripts/` into `system/libs/automations/`, matching the workspace vocabulary (an "automation" is a skill run on a schedule).

`run_job.sh` (the durable completion-tracked runner) and `with_agent_env.sh` (the cron agent-env wrapper) move over unchanged apart from path references; `run_schedule_agent.sh` becomes `run_automation.sh`, with the singleton label renamed from `schedule_agent=<skill>` to `automation=<skill>` and the default create template from `schedule_agent` to `automation`.

Existing hosts are not migrated (this rename rides the forced new-host cutover); the enable-caretaker and manage-scheduled-tasks skills write the new paths.
