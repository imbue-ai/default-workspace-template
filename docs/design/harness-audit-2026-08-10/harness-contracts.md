# Cross-harness invariants and contracts

What every harness (claude, codex, pi) and every harness operation must guarantee,
as UX-observable statements. Each invariant carries its enforcement status:
**[T]** = a test/ratchet exists that fails on violation, **[NONE]** = no enforcement
yet (a wish, not a guarantee), **[BROKEN]** = currently violated (see
findings-and-fix-plan.md). Candidates for promotion into the mngr behaviors corpus
(Gherkin Rule blocks) once stable.

## U — universal invariants (all harnesses, all operations)

- **U1 Message conservation.** A message accepted by the UI is exactly one of:
  delivered (appears as a user turn), visibly queued, or visibly returned to the
  composer. Never a fourth state; never silently lost. [BROKEN: claude restart-drain
  race — P1.6] [NONE otherwise: needs a ledger-conservation test (enqueue = dequeue +
  returned) per harness]
- **U2 Stop wins, bounded.** From stop click to no-more-output is <= its budget
  (see table); a stop never corrupts an in-flight send (the send completes or is
  returned, per U1). [T: queue-sweep suite for pi/codex; BROKEN for claude nonempty
  branch; unbounded lock wait on chord path]
- **U3 Create converges.** `create` ends in exactly one of: a ready agent, or a
  clean failure with nothing left registered/running. Reported failure with a live
  agent (zombie) is forbidden. [BROKEN — P0.2] [NONE: needs a failed-create
  leaves-no-trace test per harness]
- **U4 Destroy is total.** After destroy: no process with the agent's MNGR_AGENT_ID,
  no tmux session, no registry entry; preserved data is complete per the plugin's
  preservation manifest (a declared-preserved store is non-empty if the source was).
  [BROKEN: orphan sweep times out under gVisor; pi sessions preserved empty]
- **U5 Transcript exactly-once.** Every user-visible turn (user msg, assistant msg,
  tool call, tool result) appears in the common transcript exactly once — no drops,
  no duplicates, correctly paired/attributed. Schema validity is necessary but NOT
  sufficient; conformance fixtures must come from the binary version actually
  shipped. [BROKEN: codex drops all tool activity + duplicates; claude 'unknown'
  tool names] [T after fix: real-fixture conformance + a native-vs-common diff test]
- **U6 Disk is truth; display converges.** Chat rendering derives from on-disk
  session records; any transient display state (mirrors, caches, replays) converges
  to disk within one poll cycle and survives backend restart without inventing or
  hiding records (no ghost queue entries). No synthetic records are ever injected
  into chat surfaces. [BROKEN: ghost replays via resume — P1.9]
- **U7 Timeouts are owned and stated.** Every wait has an explicit budget owned by
  the harness plugin (which knows its runtime's worst case), never silently
  overridden by a generic caller default; budgets sit in named constants, mirrored
  where the UI needs them. [BROKEN: pi 30s budget dead code under generic 10s;
  UI 30s destroy cap vs real mngr latency]
- **U8 Failures surface with cause.** Any operation the user can trigger reports
  failure with the underlying reason (timeout != signed-out != crash); HTTP errors
  keep their status (404/405 never become 500). [BROKEN: Flask handler; auth-probe
  fail-closed 400]

## Per-operation contracts

| Operation | Contract (UX-observable) | Budget | Status |
|---|---|---|---|
| send | Accepted send appears as queued chip or delivered turn within one poll; composer text is never mangled by harness placeholders | <= 2s to visible | [T partial: delivery events; placeholder bug live] |
| queue | Queued messages persist across UI reloads and backend restarts; order preserved; each eventually delivered or returned (U1) | — | [NONE: needs restart-replay test] |
| flush (shoulder tap) | Flush delivers ALL queued messages in order to the running turn, or delivers none and reports why; flush never races a concurrent send (writers hold the message lock) | <= 16s turn-confirm | [T: claude tap suite; lock gap for pi/codex flush writers] |
| interrupt / stop | U2. Empty queue: native interrupt (claude chord / codex retract / pi sentinel). Nonempty: retract-to-composer, captured under the message lock (U1). Markers cleared so lifecycle reads idle | lock wait 2.0s; stop <= ~5s | [T pi/codex; BROKEN claude nonempty + codex markers] |
| create | U3; readiness budget owned per-harness (U7); UI shows progress immediately (proto-agent), not a blocked call | claude ~15s; codex ~30s; pi >= 120s first boot / ~30s warm | [BROKEN pi; NONE: per-harness readiness tests] |
| destroy | U4; UI destroy succeeds whenever CLI destroy would; data preserved per manifest before removal | UI cap >= p99 CLI destroy | [BROKEN: cap + sweep + pi preservation] |
| transcript | U5 + U6; `mngr transcript` output contains no plumbing pseudo-turns and no "(no content)" turns | emit within one poll (5s) | [BROKEN: see P2] |
| model/effort/fast | A switch that reports success is live within one statusline cycle AND survives restart/resume; unsupported axes are absent from the UI, not silently ignored; state file schema uniform across harnesses | apply <= 5min overlay window | [BROKEN: claude restart revert; fast-toggle 400; pi switch false-success] |
| errors | U8 everywhere | — | [BROKEN: 500-wrapping] |

## Per-harness deltas (only legitimate differences)

| Concern | claude | codex | pi |
|---|---|---|---|
| Native interrupt | tmux chord keybinding | control line in rollout jsonl (patched binary) | inbox sentinel; extension defers until injected steers park (bounded — P1.11) |
| Restart semantics | `--resume` SAME session file (queue replay must scope by process epoch) | `codex resume <id>` new rollout file | session resume via pi_session_file |
| Readiness signal | first session JSONL record | process + rollout presence | `pi_session_started` sentinel from lifecycle extension |
| First-boot cost | ~5s | ~10s | ~55-76s until npm install moves to provisioning |
| Transcript source | native session JSONL -> converter | rollout JSONL -> streamer -> converter (single-writer offsets required) | lifecycle extension emits directly |
| Model/effort/fast | statusline writes state; switch via settings (must persist model+effort, not just fast) | patched binary writes state; `/model`+`/fast` slash; durability across resume unverified | extension mailbox; no effort axis; no create-time knob |
| Policy hooks | native claude hooks | patched-binary hook system | lifecycle extension handlers |

## Enforcement backlog (ranked)

1. Ledger-conservation test per harness (U1) — enqueue = delivered + returned, run
   across stop/flush/restart storms.
2. Real-binary transcript fixtures (U5) — capture from the shipped codex 0.146 and
   pi; diff native vs common transcript in CI.
3. Failed-create-leaves-no-trace + readiness-budget tests per harness (U3/U7).
4. Destroy-totality test: no MNGR_AGENT_ID processes, preserved stores non-empty
   when source was (U4).
5. Restart-durability tests for model/effort/fast per harness.
6. Backend-restart queue-replay test (U6, ghost prevention).
7. Promote U1-U8 into the behaviors corpus as Rule blocks with witness back-links.
