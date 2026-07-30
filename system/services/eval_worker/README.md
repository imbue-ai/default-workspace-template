# eval_worker

One-shot supervised service for the minds-evals harness (`apps/mngr_minds_eval`
in the mngr monorepo). It drives a scripted multi-turn conversation with the
workspace's chat agent, snapshots the workspace home tree (`/home/user`) to R2
per turn via restic, and uploads the full transcript at the end -- so a
launched eval run completes on its own and every result is retrievable from R2
without the launching machine staying on.

## Gating

Eval mode is gated on the harness-slotted config file at
`system/services/eval_worker/test_case_metadata.json` (committed into each
case's template clone by the harness before `mngr create`). When the file is
absent -- every normal workspace -- the `[program:eval-worker]` one-shot exits
immediately and nothing else runs.

## Modules

- `responder.py` -- the entry point (`uv run eval-worker`): the turn loop,
  per-turn state writes, and the timeout/terminal-state handling. The terminal
  done marker lives at `data/.state/eval/done` so a finished (or timed-out) run
  is never re-driven when the workspace is later woken.
- `sink.py` -- the R2 (S3-compatible) sink: restic snapshots of the home tree
  plus plain-object uploads (state.json, transcript) with credentials from the
  slotted config.
- `decider.py` -- role-plays the eval client for `DECIDE_FROM_PERSONA` turns
  via the Anthropic API (harness-supplied key).
- `wait_watcher.py` -- watches the chat agent's WAITING state and sends
  messages through the local system_interface loopback API, the way the UI
  chat box does.
