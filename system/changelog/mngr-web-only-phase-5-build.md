New entry file `system/scripts/install_update_dispatcher_cron.py`: it writes the `update-self-dispatch` cron line that runs the update dispatcher, for the bootstrap, which runs it at every boot with the system `python3` and no venv. It joins the bare entry lists.

The desktop interface's contracts, section 5.7, describe the update settings route, the workspace arming its own updates from its release channel's version, the request values a client may now leave to the workspace, how a desktop that still names its newest version holds the workspace to it, the compatibility number, the shareable apps, and the profile route leaving an unchanged snapshot alone.

The state docs list the files a workspace that arms its own updates keeps under `data/.state/update-apply/`: `settings.json`, `channel-version.json`, `client-ceiling.json`, `schedule-origin.json` and `installed-version.json`.
