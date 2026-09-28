The shell owns app lifecycles (`docs/system/specs/stop-when-no-windows.md`):

- A lifecycle manager parks the loopback port of every stoppable app that is stopped and wakes the app on the first connection, answering it with a loading page that reloads until the app is up; wakes are budgeted (3 per 5 minutes per app) and a wake that ends in FATAL is answered with a "could not start" page. A POST launch to a stopped app wakes it first and waits for it to answer.

- An app whose row carries `stop_when_no_windows` is stopped once no window on any desktop has shown it for 60 seconds, and only once a client has arrived at the shell since it started. The close hint an app declares is no longer posted to a stopped app.

- `POST /api/apps/<name>/quit` closes every window of the app on every desktop and stops its program; the window menu offers "Quit <app>" in place of Stop and Start. A stoppable app's window keeps its page while the app is stopped (the parker answers it); the stopped placeholder, without its Start button, survives only for rows the shell cannot start.

- The registry read announces registrations to the minds desktop (`service_events.py`, formerly the `app-watcher` service), and the inventory's sweep re-reads the registry when its mtime moved. The `app` wire object carries `stop_when_no_windows`.
