# Phase 1 design: fast, guaranteed-ready create; fast, total destroy

Measured 2026-08-10 on this host (2-CPU gVisor). Raw timelines: /tmp/harness_probe/*/timeline.tsv
(ephemeral). Companion docs: findings-and-fix-plan.md (P0/P1 items), harness-contracts.md (U1-U8).

## Product contract (decided with Minh)

The chat tab appears ONLY when the agent is mathematically ready: TUI interactive AND
minds_model_state.json on disk (model bar populates on first paint). Until then the
existing creating page (build-log view) shows. On budget miss: clear error, agent rolled
back, no zombie. No mid-startup chat, no synthetic placeholders, first message never
does readiness double-duty. Minimal diffs; system_interface already implements the
gate (creating page flips on `mngr create` exit 0) — ZERO UI changes required.

## Measured readiness (agent-process times; CLI tax separate)

| harness | cold ready today | warm ready | model-state file | recommended ready signal |
|---|---|---|---|---|
| claude | 3.6-4.3s (session_started) | 2.9s | +3-4s AFTER ready (statusline is sole writer) | session_started + PROVISION-SEEDED minds_model_state.json |
| codex | 4.4s (pane glyph) | 1.6s | 6.4s cold / 2.6s warm (binary writes at session open) | minds_model_state.json exists, epoch-scoped |
| pi | 67.2s (sentinel; 47s = npm installs) | **8.1s** | +4ms after sentinel | minds_model_state.json exists, epoch-scoped |

Epoch-scoping: the state file survives relaunch, so "exists" means "mtime newer than the
harness's process-started marker" (or delete it in the launch prelude like session_started).
Notes: claude's session JSONL is NEVER created on a message-less boot and codex creates no
rollout file — neither is usable as a readiness signal; the model-state file is the one
uniform signal that also guarantees the model bar.

## Build list

1. **mngr CLI lazy imports** (P0.3): measured invoke-to-process-spawn tax 5.4-9.9s; target ~1s.
   Lazy click subcommand loading + defer heavy plugin imports (anthropic, paramiko, pyinfra,
   docker) to first use.
2. **Pi npm installs at provisioning** (P0.1b): cold create 67s -> ~8-10s (warm number is the
   predictor). The per-agent npm tree (33MB) materializes during provision, not first boot.
3. **create.py budget ownership + rollback** (U3/U7): stop falling back to the generic
   agent_ready_timeout (10.0s default); pass None so each plugin's constant governs. On
   AgentStartError: host.destroy_agent(agent) before re-raising — today the zombie boots
   ~1min later and flips the failed-create screen into a live chat.
4. **tui_agent.py:226 bug**: wait_for_ready_signal drops the timeout arg (codex always got
   the fixed 30s) — forward it.
5. **Readiness signals per the table**: codex + pi gate on the epoch-scoped model-state file
   (replaces the codex pane scrape); claude keeps session_started and mngr seeds
   minds_model_state.json at provision from the launch settings it already owns (model,
   effort, fastMode all known at create time; statusline reconciles afterwards). Pi: write
   the sentinel strictly after the model-state write (today +4ms in the same handler).
6. **Budgets** (fail-with-cause bounds, not experience targets): claude 15s, codex 20s,
   pi 30s. Typical experience post-fix: claude ~5s, codex ~7.5s, pi ~9-10s end-to-end.
   Pi is the only one tight against a literal 10s cold; revisit its budget after
   npm-at-provision lands and is measured.
7. **Destroy** (U4): orphan sweep becomes ONE `grep -lza "^MNGR_AGENT_ID=$id" /proc/[0-9]*/environ`
   (measured 0.05-0.1s vs 0.97-1.73s per-agent loop that blows its 10s cap under multi-destroy
   and silently skips kills); UI cap: named _DESTROY_TIMEOUT_SECONDS = 120.0 replacing the
   hardcoded 30.0 (server.py:1779); error handler `raise exc` -> `return exc` (server.py:2084)
   so 404/405 keep their status. Destroy wall time today 16.5s idle (8.4s = worktree GC);
   post-fix ~12-13s inline, ~5-6s if GC later goes async (optional, not in this phase).
8. **Release tests**: per-harness create-to-ready within budget (asserting the ready signal
   implies the model-state file); failed-create-leaves-no-trace; destroy totality (no
   MNGR_AGENT_ID processes, no tmux session, no registry entry; preserved pi store contains
   the session JSONL after a completed turn).

## Reclassified during research

- Pi preserved-sessions-empty (audit finding 15) is NOT a preservation bug: pi defers the
  session JSONL until the first assistant message; the empty preserved agents were failed
  first-boot zombies with genuinely nothing on disk. Action reduced to: warn when a preserved
  pi_session_file points at a JSONL that was not preserved + the totality release test.
- First-message double-duty wait (tui_agent.send_message -> wait_for_tui_ready) predates the
  branch, becomes a documented no-op under the create gate, and still guards restart/resume
  paths: leave it alone.
- Known accepted gap: the gate covers create only; a slow restart/revive can still surface
  mid-startup (the reverted pi-readiness-gating spec's is_ready channel is the future fix if
  it starts to matter).
