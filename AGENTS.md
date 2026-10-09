# Workspace

The workspace root is `/home/user/workspace` (run commands from there). `data/` is gitignored workspace data; an app's stored data goes in `data/.apps/<name>/`.

# Apps

An app lives in `system/apps/<name>/` (kebab-case name, also its hostname label) and is served by supervisord on its own port, bound to `127.0.0.1`. Pick a free port at or above 8080; 8000, 8010, 8030 and 8081 are taken.

1. `system/apps/<name>/app.toml`:

   ```toml
   name = "<name>"
   display_name = "<What users see>"
   icon = "icon.svg"
   priority = "user"
   program = "<name>"
   stop_when_no_windows = true
   ```

2. `system/apps/<name>/icon.svg`: a single `<svg>` element (no script, style or external references); a new registration is refused without it.

3. `system/supervisord.conf.d/<name>.conf`:

   ```ini
   [program:<name>]
   command=python3 system/services/oom_priority/bin/oom_tag_service.py user bash -c "python3 system/scripts/forward_port.py --manifest system/apps/<name>/app.toml --url http://localhost:<port> && exec <start command>"
   directory=/home/user/workspace
   autostart=true
   autorestart=true
   startsecs=30
   startretries=5
   ```

   `forward_port.py` registers the app's port and must run before the app starts. Then `supervisorctl reread && supervisorctl update`. The program reads `STARTING` for its first 30 s; `BACKOFF` or `FATAL` means it failed (see `/var/log/supervisor/<name>-stderr.log`).
