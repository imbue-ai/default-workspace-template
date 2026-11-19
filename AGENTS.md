# Showing the user a web app

The user sees a web app on this machine only once it is registered. To register one, run it as a supervisord program whose command registers its port and then starts the server. Create `system/supervisord.conf.d/<name>.conf`:

```
[program:<name>]
command=bash -c "python3 system/scripts/forward_port.py --name <name> --url http://localhost:<port> --no-icon && exec <command that starts the server on <port>>"
directory=/home/user/workspace
autostart=true
autorestart=true
```

Then load it with `supervisorctl reread && supervisorctl update`.

`<name>` is lowercase letters and digits, with single dashes between words. `<port>` is any port nothing else listens on. The app must answer `GET /` with a 200.

# Facts about this machine

- An app's stored data goes in `data/.apps/<name>/`. That directory is backed up and is not tracked by git.
- The user opens a registered app by its name, from their list of apps. A `localhost` address does not work for them.
- A picture reaches the user as a markdown image with its absolute path in your reply, such as `![Draft](/home/user/workspace/data/images/draft.png)`. Show a mockup or draft that way; register only the real app.
- Playwright's Python API is installed for looking at a page. Pass `executable_path="/opt/fortress/tilion-fortress/tilion"` to `chromium.launch(...)`.
- Work in this repository is kept in git. Commit yours when a change is done.
