Turned the single live-browser service into an agentic browser fleet: a
per-workspace daemon that manages many headless Chromium browsers, each with an
atomic ownership state machine, plus an `agentic-browser-fleet` CLI for agents to
drive them.

- Browsers now have stable integer ids (0 is the default, created on demand;
  others are monotonic and never reused). `GET /browsers` lists the fleet with
  each browser's owner and tabs; `POST /browsers` starts a new one (409 when the
  fleet is full).

- Each browser is controlled by exactly one party at a time -- a specific agent
  (by `MNGR_AGENT_ID`) or the human -- via one compare-and-set transition guarded
  by a per-browser lock. Agents never preempt each other: a `task` on a browser
  another agent holds waits in a FIFO queue until it frees (`--no-wait` fails
  fast, `--max-wait` bounds the wait). A human "Take control" always wins, pins
  the browser to the human, and ends the agent's task with a clear "lost control"
  message; the agent resumes only when the human tells it to (`--reclaim`).

- Ownership is bound to the live `task`/`hold` request connection: if the agent
  process dies or disconnects, the run is cancelled and the browser is released
  automatically, so there are no stuck locks.

- New CLI `agentic-browser-fleet` (`ls`, `new`, `task <id> "<prompt>"`,
  `lock`/`unlock`/`release`). A `task` hands a goal to a browser-use agent on the
  chosen browser and streams its thinking/action trace to the calling agent's own
  output; the browser tab is now viewer-only (the in-tab chat/composer is
  removed). The viewer shows a grey "Agent has control" overlay with a "Take
  control" button, a "Return to agents" affordance when the human is in control,
  and a clear "browser closed" state if the daemon restarts.
