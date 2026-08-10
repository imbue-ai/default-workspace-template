# Handoff: Mithril SPA migration (branch mngr/mithril-refactor, PR #257)

State as of 2026-08-02, written mid-/autofix. Read plan-minds-mithril-spa.md +
contracts.md first; this file is only the delta the next agent needs.

## Where things stand

- All build phases are COMPLETE and committed: Phase 0 (frontend scaffold +
  component catalog), Phase 1 (server /ui foundation: ws_gateway, ui_channel,
  ui_publisher, ui_models, /ui blueprint), Phase 2 (shell + route flip +
  static /login + Electron slimming + visual-diff capture-spa), tranches
  T1-T6 (all pages ported), Phase 4 (docs, changelog entries, contracts
  status note, test surgery). The task list (TaskList tool) mirrors this;
  only task #13 ("PR + CI green + /autofix + /verify-conversation") is open.
- PR: https://github.com/imbue-ai/mngr-internal/pull/257, DRAFT, base
  **josh/convert-to-iframe** (NOT main -- retargeting was required to make CI
  dispatch at all; a PR against main is CONFLICTING and GitHub silently runs
  no checks on a conflicting PR).
- CI was FULLY GREEN at commit aea899cb7d (14 pass / 0 fail / 4 skipping;
  the skips are dispatch-only minds-env jobs). This includes test-offload,
  test-offload-acceptance, gate (mirror), test-minds-snapshot (fixed by
  building the SPA bundle into the e2e image in
  scripts/snapshot_minds_e2e_state.py), test-docker, macos bundled-git.
- Manual verification: visual-diff capture-spa renders real pages with
  fixture data (checked spa_home.png -- tiles/badges/providers all correct;
  options page correctly shows its API-unavailable error posture under the
  harness). Captures in apps/minds/.visual-diff/verify-final/.

## In flight RIGHT NOW (must be picked up)

- /autofix is mid-run per the user's instruction, with the unattended
  overrides (never ask questions; unaccepted patches go to side branches
  named <branch>___<fix-description>, pushed remotely; only CRITICAL/MAJOR/
  MINOR must be fixed; unfixed issues appended to
  .reviewer/outputs/autofix/unfixed/<git-hash>.jsonl and mentioned in the
  final summary). Diff-validate passed (scope clean, 182 files, +26k/-7k).
- Autofix ITERATION 1 verify-and-fix agent is RUNNING in the background
  (agent id a1239d8a463595e45; a completion monitor `broihn5w6` watches for
  `"stop_reason":"end_turn"` in `readlink -f` of its .output symlink).
  pre_iteration_head = aea899cb7dbfc72ad5e92f36c144943255a84d25; its issues
  file is .reviewer/outputs/autofix/issues/aea899cb7d....jsonl. It has
  already found at least one real issue (a ratcheted `logger.exception` in
  ui_publisher.py:220 from a late refinement) and has modified
  apps/minds/imbue/minds/test_ratchets.py on disk (snapshot annotations for
  time_sleep=10, broad_exception_catch=11 with UiStatePublisher
  justification). Let it finish and commit; do NOT touch the tree while it
  runs.
- Remaining autofix protocol: loop verify-and-fix agents (fresh context each,
  same base/HEAD/categories inputs, max 10 iterations) until HEAD stops
  moving; then Phase 4 review of fix commits UNATTENDED per the override
  (judge each commit yourself: keep, or move to a pushed side branch);
  report unfixed issues. Then push, re-confirm CI green on the new HEAD, and
  end the turn so the stop hook runs /verify-conversation (it is
  incremental).

## Facts the next agent must know

- Base branch for ALL diffs/reviews: josh/convert-to-iframe. Repo default
  main is WRONG for this branch.
- NEVER run the full test suite locally or via offload (explicit user
  instruction -- it slows their machine). Targeted pytest
  (`--no-cov -p no:xdist`) + `cd apps/minds/frontend && pnpm build/check/
  test` only. CI (push to PR) is the full-suite runner.
- Ratchet suites (apps/minds/imbue/minds/test_ratchets.py +
  test_minds_ratchets.py) only behave on COMMITTED state (git-blame based).
  58 tests; all passed pre-autofix.
- After any ui_models.py / wire-model change: `pnpm generate` in frontend/,
  and the ui_models_test.py inline snapshot needs updating.
- Sub-agent orchestration: user requires parallel fable sub-agents for
  construction (fork subagents inherit context + model). Forks were used
  for all phases; file-ownership partitioning lives in contracts.md.
  Waiting pattern: Monitor tool polling
  `tail -c 20000 "$(readlink -f <task>.output)" | grep '"stop_reason":"end_turn"'`
  (stat the SYMLINK TARGET, not the symlink), then TaskOutput-block on the
  monitor id. Do NOT TaskOutput-block directly on agent tasks (dumps JSONL
  transcript into context).
- The stop hook demands: clean tree at turn end (WIP: commits allowed), a
  PR, then gates /autofix + /verify-conversation. Changelog entries already
  exist (apps/minds/changelog/mngr-mithril-refactor.md +
  dev/changelog/mngr-mithril-refactor.md).

## Deferred follow-ups (documented, not blockers)

Tracked in contracts.md Phase-4 status note + tranche reports; headline items:
- Phase-4b legacy deletion pass (templates/, templates.py, page-scoped
  static JS, legacy POST routes, visual_diff legacy mode) was deliberately
  deferred; legacy templates remain on disk and templates_test still covers
  them.
- Consolidate the per-area `/ui/api` auth-check copies into one shared
  before-request guard (circular-import driven duplication in
  ui_api_{create,settings,options,lifecycle,inbox}.py).
- Fold per-area API response models into the UiWireSchema codegen (tranche
  TS interfaces are hand-mirrored, pinned by pytest route tests) -- needs a
  ui_api_models layer below `state` to break the import cycle.
- Options-page error Notice should truncate raw HTML error bodies (seen in
  visual-diff capture).
- Op-log SSE -> WS consolidation is an explicit later cleanup (user
  decision), as is the full auth overhaul (templates_auth.py + SuperTokens
  surface kept alive intentionally).
- T1 kept create-POST on /api/v1/workspaces (single create front door)
  instead of a /ui/api twin with client-minted operation ids -- flagged for
  the idempotency follow-up.

## Process notes for the final summary to the user

- The T5 fork overran its scope (it also did T6, docs, changelog, CI test
  surgery, and ran git commits/pushes despite the no-git instruction, while
  "acting as orchestrator continuation" with inherited context). All of it
  was reviewed green locally and CI-green after, but it must be disclosed.
- One commit's message was observed swapped relative to what the
  orchestrator wrote ("Restore pre-check coverage..." vs the intended
  "Cover the predefined-permission..."); content was identical in effect --
  worth a mention only.
- The user's standing instructions: implement the whole plan, parallel
  fable sub-agents, /autofix at the end, PR exists, CI green, no full local
  test runs.
