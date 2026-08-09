Added codex as a peer harness in the workspace chat UI, alongside claude.

- Depends on the `imbue-mngr-pi-coding` mngr harness plugin, installed with the
  system-interface tool so `uv run mngr` parses its agent-type config.
- Chat-agent creation now selects the harness with `mngr create --type <harness>`
  (which resolves `[agent_types.<harness>]` directly) and layers only the `chat` role
  template on top, instead of stacking a per-harness create template ahead of the role.
  A harness's create template held nothing but `type`, so it was redundant with `--type`;
  dropping it removes one config block per harness with no behavior change.

- Codex transcript tool blocks and the live activity caption now describe what
  the code-mode call actually did -- "Running <cmd>", "Editing <file>",
  "Searching the web" -- instead of an opaque "Tool: exec", from a single shared
  label (so header and caption never disagree).
- The bottom activity indicator is driven by codex's own turn markers
  (task_started / task_complete), so "Thinking..." brackets the real turn; codex
  tool captions are briefly debounced so fast code-mode calls don't flicker.
- Sending to a cold codex agent no longer hangs the request past the proxy
  timeout ("Failed to send: null"): the send is bounded and, if the agent is
  still starting, accepted for background delivery.
- Every non-claude launcher ("New Codex/Pi Agent") is gated
  behind a single FEATURE_FLAG_ENABLE_OTHER_HARNESSES feature flag (off by
  default), so the alt harnesses can be dark-launched and enabled per host without
  a rebuild. (Generalizes the earlier codex-only FEATURE_FLAG_ENABLE_CODEX flag.)
- Creating an alt-harness agent now runs that harness's own CLI sign-in check
  first (codex/pi) and refuses the create with a readable message
  when the harness is signed out, so a chat that could never take a turn is never
  launched.
- Fixed: a codex agent interrupted mid-turn and resumed could stay stuck on
  "Thinking..." until the user sent another message. The restart-boundary check
  looked for claude's marker file on every agent, so it never fired for codex
  (which writes its own).

- pi's tool labels now name the `pi-web-access` extension's tools instead of
  showing a raw `Tool: web_search` / "Running ...". The four tools read like
  their claude/codex peers -- `web_search` -> "Tool: WebSearch" / "Searching the
  web <query>", `fetch_content` -> "Tool: WebFetch" / "Fetching page <url>",
  plus "Checking sources <claim>" (`source_check`) and "Retrieving results"
  (`get_search_content`). Names verified live against pi-web-access 0.19.0; they
  are the package defaults, so a `web-search.json` `toolNames` override renames
  them.

- The pi queued-message mirror is now scoped to the live process generation: a
  drained `user_message` only pops a queued entry when its timestamp is at or
  after the `pi_process_started` marker's mtime, so a dead generation's drains
  (replayed from the durable session file after a backend restart) can no longer
  eat current-generation entries or leave phantom "queued" residue. Pairs with
  the mngr-side pi extension change that archives `pi_inbox` to
  `pi_inbox_history` and truncates it at load, which generation-scopes the
  enqueue side by construction.

The claude "Shoulder tap" now flushes queued messages into the live turn natively, instead
of the SIGKILL-restart-and-resend it fell back to before. It cancels claude's running turn
via a Chat-only meta+q -> chat:cancel chord (provisioned by mngr), which makes claude commit
its parked queue as a fresh merged turn -- the same auto-flush it does at natural turn end.
The keypress is delivered through mngr's in-process message API (holding the per-agent message
lock, so it never interleaves with a text send); dwt never drives raw tmux. A short bounded
watch confirms the flush went through and, in the rare race where the chord also cancels the
just-flushed follow-on turn, sends one recovery nudge so the committed messages are still
addressed. The agent is never restarted and the queue mirror is never torn down. The tap
button now also releases the "Sending queued messages..." freeze immediately on a terminal
no-op (nothing was queued, or no turn was running) instead of holding it to the fallback cap.
