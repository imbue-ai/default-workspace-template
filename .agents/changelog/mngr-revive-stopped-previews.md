- `serve_isolated_instance.py revive --name <name>` boots again whatever part of an isolated instance has stopped: the app from its recorded recipe, the preview frame from its recorded arguments, a mock's frame from its record, all on their recorded ports and over the same copies, so an open window works again. Running parts are left alone, the siblings a preview frames come back first, and it refuses a recorded port another process now holds or a record from before this change.

- `up` now records each process's start time, so a process a restart handed the same pid is never taken for the instance's own, along with the frame's arguments and the siblings to revive first.

- The row a preview's or mock frame's window shows now names its instance and kind (`isolated_instance`), which is how the shell offers **Start it again** in the window. The update-app skill says so and gives the `revive` command for agents.
