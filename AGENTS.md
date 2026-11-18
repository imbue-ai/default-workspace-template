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
