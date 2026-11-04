- `system/scripts/smoketest_app.py` is renamed `verify_app.py`, since it restarts, checks, renders and surfaces an app rather than only smoke-testing it; every skill, reference and README that named the script now uses the new name.

- `verify_app.py <name> --restart` bounces an app's supervisord program without waiting out its `startsecs` window: it starts the program through supervisord's RPC socket without waiting, reads readiness from the port, and fails at once with the last lines of the stderr log when the program dies on start. `--open` and `--refresh` surface the verified page through `workspace-layout` in the same call.

- `AGENTS.md` makes app verification in a chat turn one `verify_app.py --restart ... --open|--refresh` call and leaves new test files to the background harden pass (a change to hardened code still extends its tests) and suite runs to the background test gate.

- The standalone-tk hook allows a tool call made only of `tk create`, `start` and `close` commands joined by `;`, `&&` or a newline, so a step boundary (`tk close <id> "<summary>"; tk start <next>`) is one tool call. A `cd` prefix, real work, a pipe, `||`, `&` or a redirect alongside a start or close is still blocked. `AGENTS.md` has the agent start a step in the same message as its first command and move between steps with one `tk close ...; tk start ...` call.

- The pipe-into-head/tail hook reads through a line continuation after the pipe, so `cmd | \` followed by `tail -30` on the next line reaches the checker instead of running unchecked.

- `verify_app.py --restart` reaches supervisord on the socket named by `MINDS_SUPERVISOR_SOCKET` when that variable is set (the shell app's liveness probes read the same one), else on `/var/run/supervisor.sock`.
