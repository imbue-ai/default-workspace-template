`GET /new[?url=]` is the browser's launch path: it creates a browser and redirects to its viewer page at `/?session=<name>`.

The viewer page imports the workspace shell's app contract from the shell origin (whose label the daemon stamps into the page) and reports its path and the browser's name as its title. It declares no navigation capability: a session switch is a whole new stream, so the shell reloads the frame to move it.
