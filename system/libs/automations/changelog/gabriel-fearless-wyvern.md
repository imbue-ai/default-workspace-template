`with_agent_env.sh` moved out of this package to `system/scripts/with_agent_env.sh`; sshd scrubs the environment the same way cron does, so the wrapper is shared infrastructure rather than an automations detail.

Cron entries scheduled before the move still name the old path (they live in gitignored `data/.state/cron.d/` and survive `update-self`), so a shim remains here and execs the new location. The `run_automation.sh` comment and the package README point at the new path.
