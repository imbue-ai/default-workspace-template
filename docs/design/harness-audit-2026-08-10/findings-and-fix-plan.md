# Harness audit — findings and fix plan (2026-08-10)

Full-codebase audit of the claude/codex/pi harness work on `claude-codex-pi-dwt`.
Method: 10 parallel investigators (each area below), adversarial verification of every
critical/high defect claim (7/7 independently CONFIRMED), plus two follow-up
investigations (claude chat message drops; model/effort/fast pickers). Raw per-agent
reports: `~/.claude/projects/-mngr-vol-home-workspace/b5bfe749-*/subagents/workflows/wf_b4d24fa9-659/journal.jsonl`.

Verdict in one line: the queue/interrupt/shoulder-tap core is sound and verified live;
the damage is concentrated in timeout arithmetic, startup latency, transcript emitters,
one missing lock on claude's stop path, and test blind spots.

## P0 — restore the basics (create, destroy, speed)

1. **Pi create always fails on first boot** (CRITICAL, confirmed by live repro twice).
   `create.py:412-421` (commit 0f0f200b) waits on readiness with
   `MngrConfig.agent_ready_timeout` default **10.0s**; the pi plugin's own
   `_READY_TIMEOUT_SECONDS = 30.0` (`mngr_pi_coding/plugin.py:144`) is dead code on this
   path; pi's real time-to-sentinel is **55–76s** (two sequential per-agent npm installs;
   the `~1s` comment at plugin.py:86-87 is off ~45x).
   Fix: (a) harness-owned budgets — `create.py` passes `None` unless explicitly
   configured, plugins apply their own constant; raise pi's to >=120s; (b) move the
   npm install into provisioning so first interactive boot is fast.
2. **Failed create leaves a live zombie agent** (HIGH, confirmed; the agent finishes
   booting ~1min later while `mngr create` reported failure). Cleanup at
   `create.py:436-438` never fires for chat-template creates (`transfer="none"` path).
   Fix: on readiness timeout, either roll back the registered agent or report
   "still starting" instead of failure and let the UI reconcile when the sentinel lands.
3. **Every mngr invocation pays a fixed startup tax** (HIGH, confirmed: ~6–9.5s warm,
   12.5s cold, worse under load; baseline `uv run python -c pass` 0.48s). `main.py`
   eagerly imports every subcommand and `load_setuptools_entrypoints("mngr")` eagerly
   imports every plugin (anthropic, paramiko, pyinfra, docker...) over a 9p filesystem.
   Fix: lazy click subcommand loading + defer heavy imports to first use inside
   plugins. (Later option: resident daemon for the UI's mngr calls.)
4. **UI destroy 500s** — three stacked causes, all confirmed live:
   the 30s subprocess cap trips over (3); the destroy orphan-process sweep (a `/proc`
   environ scan) times out at its own 10s cap under gVisor so leftover processes
   survive; and the Flask error handler converts HTTPExceptions (404/405) into 500s.
   Fix: after (3), raise/instrument the cap, batch or lengthen the sweep, and
   `return exc` for HTTPException in the handler.
5. **UI create feels slow for every harness** (confirmed): the new synchronous readiness
   wait sits on the UI's only create path, on top of (3), plus a ~4 req/s poll storm
   and a synchronous uncached 20s CLI auth probe that once rejected a valid create.
   Fix: async readiness via the existing observe stream (proto-agent immediately),
   poll backoff, cached auth probe with "could not verify" != "signed out".

## P1 — close the contract holes (the "stake my life" gaps)

6. **Claude nonempty-queue stop takes no message lock** (the one confirmed way a chat
   message is lost outright, silently). `restart_drain` captures a stale mirror and
   SIGKILLs without holding `message.lock`; the dda98ed0 bounded-lock fix covered only
   pi/codex. Fix: extend the same bounded-lock design to claude's restart-drain branch
   (refresh mirror + capture under lock).
7. **Claude chord path acquires the lock unbounded** (stop can stall ~90s; pi/codex
   bound it at 2s). Fix: `try_hold_message_lock` + fall back to restart-drain.
8. **Codex native retract strands `active`/`codex_root_active` markers** — lifecycle
   reports RUNNING indefinitely after a stop. Fix: clear markers on the retract path
   as claude's chord does.
9. **Ghost queue entries on replay**: the watcher assumes restart rotates the session
   file; `--resume` re-appends to the same file, so retracted enqueues replay as
   "queued" then silently evaporate (the user-visible disappear/reappear). Fix: scope
   queue replay by process epoch (enqueue ts vs `claude_process_started` mtime).
10. **Watcher discovery gap**: up to ~60s blind window at creation, with an inline
    0.5s sleep on the HTTP read path stalling `/events`; late-found sessions appended
    out of order. Fix: non-blocking pending-session registration, ordered insert.
11. **Pi extension robustness**: a never-settling steer injection defers a
    retract/flush sentinel forever (unstoppable turn) — add bounded deferral;
    `injectSteer` thenable guard checks `.then` but calls `.catch/.finally` — wrap in
    `Promise.resolve()`; flush writers (pi sentinel, codex control line) should take
    the message lock instead of relying on frontend button greying.

## P2 — transcripts

12. **Codex converter drops ALL tool calls/results** (HIGH, confirmed): only handles
    pre-0.146 `function_call` shapes; the patched binary emits `custom_tool_call`.
    Fix: add the branches + capture a real 0.146 code-mode fixture. This also closes
    the conformance blind spot (schema-valid-but-empty passed green tests).
13. **Codex streamer duplicates lines** (confirmed, bidirectional race between the 1s
    daemon and turn-end single-pass over per-rollout offsets). Fix: one writer —
    serialize both through the existing convert lock or persist offsets atomically
    before emit.
14. **Claude converter defects** (mngr-pipeline side, chat UI unaffected): tool
    results labeled `unknown` when converted a pass after their call (dedup `continue`
    skips map-building — build the map before the skip); thinking-only messages emit
    empty records rendered "(no content)" (skip them); slash-command plumbing leaks
    as fake turns (filter like the UI parser does). NOTE (user rule): no synthetic
    messages are to be added to chat surfaces as part of any of this.
15. **Pi sessions are preserved empty** (HIGH): `plugin/pi_coding/sessions` declared
    preserved but every preserved pi agent has the tree with zero JSONLs while
    `pi_session_file` proves sessions existed — recovered pi agents lose their
    conversation. Root-cause the copy path (empty at destroy time vs preservation bug).
16. **AGENTS.md injection appears as a giant fake user message** in codex common
    transcripts — tag/skip instruction-injection turns in the converter.

## P3 — model / effort / fast pickers

17. **Claude model/effort switches do not survive restart** (launch settings re-pin
    `opus[1m]` every relaunch; only `fastMode` is recorded per-agent). Fix: record
    model+effort in the same per-agent settings file. (Caveat: restart precedence
    inferred, not yet observed — verify with a real restart first.)
18. Fast toggle 400s when live effort is null (skip the effort-required guard when the
    axis isn't in the request); workspace fast answer never reaches codex launches
    despite `supports_fast=True`; codex switch durability across `codex resume` is
    unverified (patched-binary behavior — test it); pi has no create-time model/effort
    knobs at all and its switch reports success even when the extension drops the
    model. Create-time selection is three unrelated mechanisms with no shared mngr
    abstraction — unify when touching this area.

## P4 — gates, hygiene, environment (mostly quick)

19. **CI-blocking, in the change surface (fix first, cheap):** 4 ty errors — including
    a real latent crash: `ensure_chat_cancel_tap_keybinding` TypeErrors on a non-dict
    `bindings` value (`claude_config.py:582-583`) — plus 5 unformatted files/unsorted
    imports, a new silent `JSONDecodeError` swallow (log it), a trailing comment
    ratchet hit.
20. **Environment-dependent test failures** (all deterministic, mechanisms identified):
    21 system_interface e2e tests error (Playwright headless shell not provisioned —
    install in bootstrap or reuse Fortress); 2 issue_reporting tests don't pin
    `IS_AUTONOMOUS`; 6 install-script + 3 modal-deploy tests resolve repo root by
    nearest `.git` and escape the vendored checkout; `test_prevent_bash_without_strict_mode`
    rglob-walks the vendored 30k-file venv into its 10s timeout (prune dirs / scan
    tracked files only); retired-terminology ratchet regressed via new design docs.
21. **FLAKY TEST (must fix soon):** `server_test.py` proto-agent-logs websocket tests
    fail most runs with `OSError: Bad file descriptor` (teardown race) — guard the
    close. This is the only flakiness found; everything else reproduced
    deterministically.
22. **Uncommitted supervisord.conf flag** (`FEATURE_FLAG_ENABLE_OTHER_HARNESSES=1`,
    live in the running process, contradicting the deliberate 8e0a4c5e revert):
    after P0.1 lands, commit it with rationale; until then it exposes a launcher that
    fails. Decide explicitly either way.
23. Dedup: `message.lock` filename + lock helper shared from `imbue.mngr` instead of
    copied into system_interface; tk-guard reminder text triplicated across shell
    hooks and the TS extension; `try_hold_message_lock` None-default exists only for
    test patching. Docs: mark both shoulder-tap plans SHIPPED/SUPERSEDED, fix the
    HarnessCatalog "True only for codex" comment, index `docs/design/` in docs README.
24. Notes for operating here: the vendored mngr venv needs its own
    `uv sync --all-packages` (workspace-root sync does not cover it); the fast subset
    of the mngr suite is ~17.8k tests (~60min on this 2-CPU box); a host-wide pytest
    lock serializes all runs. Suite state at audit time: **17,839 passed / 16 failed**
    (7 real quality-gate failures = items 19; 9 environment = items 20).

## Sequencing

Recommended order: 19 (unbreak gates) -> P0.1+P0.2 (pi create + zombie) -> P0.3
(startup tax) -> P0.4/P0.5 (destroy + create UX) -> P1 (locks/markers/ghosts) ->
P2 (transcripts) -> 22 (commit the flag deliberately) -> P3 -> remaining P4.
Every fix lands with the test that would have caught it (see harness-contracts.md
for the invariant each maps to). Branch rule: commits on `claude-codex-pi-dwt` /
`*-mngr` branches only; never push `main`.
