- `smoketest_app.py <name> --restart` bounces an app's supervisord program without waiting out its `startsecs` window: it starts the program through supervisord's RPC socket without waiting, reads readiness from the port, and fails at once with the last lines of the stderr log when the program dies on start. `--open` and `--refresh` surface the verified page through `workspace-layout` in the same call.

- The standalone-tk hook allows a tool call made only of `tk create`, `start` and `close` commands joined by `;`, `&&` or a newline, so a step boundary (`tk close <id> "<summary>"; tk start <next>`) is one tool call. A `cd` prefix, real work, a pipe, `||`, `&` or a redirect alongside a start or close is still blocked.

- `AGENTS.md` makes app verification in a chat turn one `smoketest_app.py --restart ... --open|--refresh` call, leaves test files and suite runs to the background harden pass, and moves between steps with one `tk close ...; tk start ...` call.
