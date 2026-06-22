---
name: build-web-service-worker
description: Harden a freshly built web service (a scaffolded Flask lib under libs/) in an isolated worktree -- write thorough Playwright tests, run the full suite and ratchets, run the review gates -- then report back. Invoke when your task file asks you to finalize a newly built web service.
metadata:
  role: worker-sub-skill
---

# Finalizing a freshly built web service

This is the **service specialization of crystallization**. Follow
`.agents/shared/references/crystallize-artifact.md` for the generic contract --
the premise (the thorough pass the lead deferred), the bar (genuinely
well-tested and clean before you report `done`, not "it ran once"), working in
your own worktree, reporting back, the testing/hardening contract, the review
gates, and the give-up path. This sub-skill adds the web-specific parts below.

Your task file points at a web service the lead already built and confirmed with
the user in the foreground: a scaffolded Flask lib under `libs/<package>/`,
registered in `supervisord.conf`, reachable at `/service/<name>/`. The user has
already signed off on how it looks and works. The artifact already exists on
disk, so nothing needs reconstructing from the transcript -- your job is to prove
it works under test, harden it, and pass the review gates, without touching the
live service until the lead merges.

## Reporting back to the lead

Follow `.agents/shared/references/worker-reporting.md` for the report-file
procedure and the task-file frontmatter schema (`lead_agent` /
`finish_report_path`). Substitutions for this flow:

- `<TASK_FILE_GLOB>` -> `runtime/launch-task/*/task.md`
- `<RUNTIME_REPORTS_DIR>` -> the directory part of `finish_report_path`
  (i.e. `dirname "$FINISH_REPORT_PATH"`).
- Valid `name:` values: `question` (mid-flight gate), `done` / `stuck`
  (terminal).

For a mid-flight `question` gate, stop your turn after pushing -- the lead replies
via `mngr message` and you resume. For terminal statuses, the run ends.

## Where the source lives

- The scaffolded lib: `libs/<package>/src/<package>/runner.py` (the Flask app
  and routes) plus its `pyproject.toml`, `README.md`, and
  `test_<package>_ratchets.py`. Your task file names the exact package and
  service name.
- The service entry in `supervisord.conf` (and the matching root `pyproject.toml`
  workspace wiring). You normally do not need to touch these -- the lead's build
  created them.

## How to run and test it (in-process / isolated, never the live service)

- Your fresh worktree has no `.venv` (it is gitignored), so run
  `uv sync --all-packages` once before any `uv run`.
- If a fix needs a new dependency, add it the normal way (`uv add ...`) and
  **commit the manifest changes** (`pyproject.toml` / `uv.lock`) on your branch so
  they reach the lead in the merge.
- Exercise the app **in-process** -- drive the Flask app with its test client
  (`app.test_client()`), or launch a **throwaway** threaded Werkzeug server
  (`run_simple(..., threaded=True)`) on an alternate port (never `8000`, never the
  service's live port). Never restart or curl the live `svc-<name>` window.
- For browser-level verification, drive Playwright against that isolated instance.
  The `build-web-service` skill's `references/verify.md` describes the
  curl-then-Playwright recipe; adapt it to your isolated port rather than the live
  proxy.

## Testing contract (web specifics)

Apply the crystallization contract's testing/hardening and review-gate sections,
with these web specifics:

- The real routes are what you test -- assert on markers true if and only if each
  route behaves correctly (status, rendered content, the raw-data/source
  affordance, empty and overflow states). Add a `test_<package>.py`, plus
  Playwright coverage where the value is in the rendered UI, not just the JSON.
- Run every suite that applies: `cd libs/<package> && uv run pytest` (or the
  repo-root invocation the project uses), plus the ratchets in
  `test_<package>_ratchets.py`.
- Your `subskill-worker` template already enables the autofix + CI + architecture
  gates the contract requires; report `done` only when all tests and gates pass.

## What you must NOT do

- Do not restart `svc-<name>`, do not run `layout.py open`/`refresh`/`list`
  against the served tree, and do not try to "reveal" your work. Revealing a new
  service is trivial and is the **lead's** job after merge (a tab refresh) -- it
  is not the life-or-death live-UI reveal that `update-system-interface` needs, so
  there is deliberately no reveal/rollback machinery here.
- Do not touch `apps/system_interface` or `libs/web_server/`.
- Your job ends at a committed, verified branch. The lead merges it and refreshes
  the user's tab.

## If you need to give up

Emit a `stuck` terminal report per the crystallization contract, stating what
blocked you and where the work stands -- e.g. a dependency you cannot reach, or a
route whose intended behavior is underspecified and you cannot resolve it from
the task file. Do not report `done` on a service whose tests or gates do not
pass.
