- `system/test_web_client_routes.py` holds the share gateway and the shell to one another on what the hosted web client's page may call: the path prefix, the methods the browser's permission check allows, and the name of the mark on the request identity.

New entry file `system/scripts/install_update_dispatcher_cron.py`: it writes the `update-self-dispatch` cron line that runs the update dispatcher, for the bootstrap, which runs it at every boot with the system `python3` and no venv. It joins the bare entry lists.

The desktop interface's contracts, section 5.7, describe the update settings route, the workspace arming its own updates from its release channel's version, the request values a client may now leave to the workspace, how a desktop that still names its newest version holds the workspace to it, the compatibility number, the shareable apps, and the profile route leaving an unchanged snapshot alone.

The state docs list the files a workspace that arms its own updates keeps under `data/.state/update-apply/`: `settings.json`, `channel-version.json`, `client-ceiling.json`, `schedule-origin.json` and `installed-version.json`.
