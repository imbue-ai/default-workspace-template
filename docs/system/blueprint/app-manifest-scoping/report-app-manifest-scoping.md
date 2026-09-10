# Report: app manifest scoping

Status as of 2026-09-10. Branch `mark/app-manifest-scoping`, pull request #570 (`19be2c213` plan, `1c90bee0d` implementation, `494601659` and `233d2b626` review fixes). One end-to-end eval run against `1c90bee0d`, and a six-arm measurement of the two review invocations on a toy fixture.

## What was delivered

| Piece | Where | State |
|---|---|---|
| `[[references]]` and `[scope] exclude` in `app.toml`, validated by the library, ignored by registration | `system/libs/app_manifest/` | done, 172 unit tests |
| `app-manifest footprint` (scope file), `footprint --for-path`, `references --for-path`, `validate-manifest --repo-root` | `system/libs/app_manifest/src/app_manifest/{scope,cli}.py` | done |
| Repo-wide check that every manifest's references exist | `system/test_app_manifests.py` | done |
| Harden worker writes the scope file, tests over the footprint, settles `outside_footprint`, regenerates before reporting | `.agents/shared/worker/references/{harden-creation,type-app,type-skill}.md` | done |
| Leads record `scope_file` and `diff_base`; freshness check and publish-template read the footprint | `.agents/skills/{crystallize,update,heal}-creation`, `harden-contention.md`, `publish-template` | done |
| Review invocations carry the scope brief through `$ARGUMENTS` | `verification.md` | written, unloaded (gates stay parked) |
| Toy fixture: `toy_notes` app, `toy-notes-digest` skill referenced from its manifest, a branch renaming the route in the app only | worktree `/Users/markally/imbue/wt-app-manifest-scoping-toy`, branches `toy-base`, `toy-unscoped`, `toy-scoped`, `toy-noref-base`, `toy-noref` | built, not merged |

Tests: library 172 passed; root suite 2,345 passed with the 9 known macOS-only `agy_shim` failures; the same suites pass on Linux in a container except tests needing `jq` (absent from that image). Changelog gate ok. CI has not run yet.

## The success criteria and the evidence for each

The criteria set at the start:

1. When a user has or creates artifacts outside an app's directory and then creates or updates the app, the review stage considers those artifacts.
2. The set of things considered in review is narrower than before: the code changes plus the manifest's references, with room to expand when the agent needs to. Measurable in a toy setting as files referenced and amount read.

### The toy measurement

Fixture: a scaffolded `toy_notes` Flask app with `GET`/`POST /api/notes`, a `toy-notes-digest` skill whose `run.py` calls that route, the skill referenced from the app's `app.toml` with a note naming the route, and a one-commit branch that renames the route to `/api/entries` in the app only (2 files, 7 lines). Each arm ran one review invocation by hand with `claude -p` in the toy worktree, base branch pinned through `CODE_GUARDIAN_STOP_HOOK__BASE_BRANCH`, and every file the main agent or its two subagents read was counted from the session transcripts (`~/.claude/projects/-Users-markally-imbue-wt-app-manifest-scoping-toy/`). The agents read almost everything through Bash (`cat`, `git show`, `grep`), so "files read" counts repo files a shell command named, plus `Read` calls; "repo-wide greps" counts `grep -r` over the tree; "lines returned" is the text those calls brought back.

Two things vary between arms, and the table's `Reference` and `Brief` columns name them:

- **Reference** is whether the app's `app.toml` carries the `[[references]]` entry pointing at the skill (`path = ".agents/skills/toy-notes-digest"`, `note = "Digests the saved notes on demand; calls GET /api/notes"`). `no` is the pre-manifest world: branch `toy-noref`, whose base commit strips the entry, so nothing in the tree links the app to its consumer. `yes` is the branch as built (`toy-unscoped` / `toy-scoped`).
- **Brief** is the text handed to the review skill through `$ARGUMENTS`, which the skill pastes into its sub-agent's instructions. `old` is the pre-branch `{creation_context}` paragraph from `verification.md` at `1c90bee0d^` ("judge it against the conventions for its type ... not against `system_interface`'s patterns ..."), which names two convention docs and sets no reading bound. `scope brief` is the branch's replacement: the same opening sentence, then the scope file's path, the ordered read list (diff, `primary`, `wiring`, `references`, `context`, `conventions`), the `exclude` rule, the expansion rule with the `Expanded to:` reporting requirement, and the consumer-contract check.

The three combinations run: no reference with the old brief (the control, what a workspace had before this branch); reference with the old brief (the manifest alone, no instruction to use it); reference with the scope brief (the branch as designed). The fourth combination, scope brief without a reference, was not run: the brief reads the scope file, and a scope file with no references is the same footprint the old brief's two docs already describe.

| Invocation | Reference | Brief | Files read | In footprint | Outside | Repo-wide greps | Lines returned | Tests run, in footprint | Tests run, outside | Wall s | Cost $ | Found the stale skill |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| verify-architecture | no | old | 13 | 10 | 1 | 1 | 692 | app | none | 238 | 1.59 | yes |
| verify-architecture | yes | old | 15 | 10 | 3 | 3 | 971 | app (twice), skill | none | 600 | 1.77 | yes |
| verify-architecture | yes | scope brief | 17 | 13 | 1 | 1 | 730 | none (drove the app's test client instead) | none | 228 | 1.45 | yes |
| autofix | no | old | 12 | 7 | 3 | 3 | 679 | app (twice), skill | whole root suite (2,300+ tests); `agy_shim` suite in a base-branch worktree | 372 | 2.17 | yes, 2 fix commits |
| autofix | yes | old | 12 | 6 | 4 | 3 | 826 | app (twice) | `test_app_manifests.py` | 402 | 2.36 | yes, 4 fix commits |
| autofix | yes | scope brief | 16 | 10 | 2 | 1 | 1063 | app, skill | `test_app_manifests.py` | 386 | 2.60 | yes, 3 fix commits |

"In footprint" counts files under `primary`, `wiring`, `references`, or the three convention docs; "outside" is everything else in the repo except `.reviewer/` and `data/`. The two "tests run" columns list every pytest invocation the arm made: "app" is `cd system/apps/toy_notes && uv run pytest` (16 tests), "skill" is `uv run pytest .agents/skills/toy-notes-digest` (2 tests); `test_app_manifests.py` sits outside the footprint but is the repo-wide check of the manifest the branch touches.

What the numbers say:

- **Criterion 1 is not separated by this fixture.** Every arm found the stale skill, including the control with no reference. The control located it by listing `.agents/skills/` (the skill's name contains the app's name) and by one repo-wide grep for `api/notes`. In a repo with one app and one skill those heuristics are as good as a declared pointer. The reference changes *how* the consumer is found (the scoped agents opened `app.toml`, read the note, then the skill; the control grepped), not *whether*.
- **Against the control, the scope brief did not change what verify-architecture read.** Control and scoped arm both read 1 file outside the footprint and ran 1 repo-wide grep; the scoped arm read 4 more files, all inside the footprint (the ratchet file, `supervisord.conf`, the style guide, the scope file). For autofix the scoped arm read 2 outside files and ran 1 grep against the control's 3 and 3, and skipped the speculative reads the control made (`update_self_test.py`, `agy_shim_test.py`, `pyproject.toml`). One run per arm, so this is a direction at best.
- **The reference-with-old-brief arm is a noise estimate, and the noise is as large as the effect.** The old brief never mentions the manifest, so adding the reference should change nothing about how the reviewer behaves; that arm nevertheless differs from the control by 2 files, 2 greps, and 360 s of wall time on verify-architecture. Single runs of these agents vary by about that much, which is the same size as any read-count difference the scope brief produced.
- **Test scope narrowed, and that is the largest observed effect.** The control autofix ran the app suite, the skill suite, then the whole root suite (2,300+ tests, three minutes), then created a worktree of the base branch to prove the 9 macOS failures pre-existed. The scoped autofix ran the app suite, the skill suite, and `system/test_app_manifests.py`. The old-text-with-reference autofix also committed a `uv.lock` fix for the fixture's unsynced lockfile, a change outside the footprint that the scoped arm read and left alone.
- **The expansion rule was followed.** Both scoped agents ended their reports with an `Expanded to:` table naming each out-of-footprint file and why (the scope file itself, `test_app_manifests.py` followed from `system/apps/README.md`, the plan doc surfaced by a grep). One wrinkle: the brief's exclude rule matches the scope file (`data/**`), and both agents flagged reading it as an expansion; `verification.md` exempts it.
- **Wall time and cost are within noise** for the verify arms (228-238 s, except the old-text-with-reference run at 600 s, of which 270 s was API time and the rest waiting on a subagent) and for the autofix arms (372-402 s). Lines returned are higher for the scoped autofix because it read `uv.lock` and the scope file in full.

So the answer to "what percentage of considered files did the manifest remove" on this fixture: none. On files read and repo-wide greps, verify-architecture shows no effect against the control, and autofix shows a difference (3 to 2 outside files, 3 to 1 greps) no larger than the run-to-run noise. The one effect that stands above the noise is autofix's test scope: the whole monorepo suite in the control, the footprint's suites in the scoped arm, which follows from the brief telling the agent where the tests are rather than from any read-count change. Repeated runs per arm (five or more) and a fixture large enough to make the control's grep expensive, with a consumer not named after the app, are what would show a difference on files read and on criterion 1.

### Criterion 1 in the eval run

The eval's to-do app had no external artifacts, so nothing was there to be considered; the delivered `app.toml` correctly has no `[[references]]`. The observed piece is the diff-classification half: the worker's report listed the 3 files the lead changed outside the app (`update_self_test.py`, `test_meta_ratchets.py`, `uv.lock`) under `Outside footprint:` with a reason each.

### Test scope in the eval run, an observed regression

The eval showed the worker running `uv run pytest` from the repo root three times (2,394 tests, vendored code included) on top of the app's own 45. The `type-app.md` command at the pinned sha degraded to a bare root run whenever no referenced directory matched, which was always, since it looked for tests at a skill's top level while skills keep them under `scripts/`. `494601659` guards the run on a non-empty list and searches beneath each referenced directory. The toy measurement above shows the scoped autofix arm staying inside the footprint's tests; the eval fix has not been observed live.

## Eval summary

`todo-app-app-manifest-scoping`, one trial, 53 minutes, no timeout. Reward 0.78: gates 1.0, harness quality 0.94, outcome 0.75 (one UI flow failed on the verification browser's mis-click, per the judge), quality 0.73. Hardening ran about 21 minutes from approval to merge. The lead wrote a real `diff_base` sha into the task file. Results: `/Users/markally/imbue/wt-mngr-app-manifest-scoping/apps/minds_evals/jobs/todo-app-app-manifest-scoping/todo-app__i6Pq2Pz/`.

The harden worker's transcript was not captured. The collector does handle a destroyed worker: `evidence_collection.py`'s `worker_capture_command` falls back to mngr's `preserved/<name>--<id>/events/*/common_transcript/events.jsonl` under `${MNGR_HOST_DIR:-/home/user/.mngr}`, and the claude agent type preserves on destroy by default (`preserve_sessions_on_destroy = True`, `on_destroy` in `mngr_claude/plugin.py`). For `crystallize-todo` the lead ran `create_worker.py destroy` (`mngr destroy --force`) about a minute before the conversation ended, `mngr transcript` then reported "Could not find agent", and the preserved-directory fallback matched nothing (`transcript.err` holds only the two lookup errors, no copy error). The capture command's section markers (`document_exit`, `stream_exit`, `preserved`) are not persisted anywhere in the trial output, so whether the preserved directory was absent, elsewhere, or named differently cannot be told from the host side. A live box with a destroyed worker is what settles it; until then, a worker's report body is the only worker output an eval keeps.

## Open items

- A fixture where the consumer is not findable by name or a cheap grep (several apps, several skills, a consumer named for what it does), to test criterion 1 and get a file-count difference.
- Persist the worker capture's section output into the trial directory so a missed preserved stream is diagnosable, then re-run the eval with a worker left alive at collection to confirm the fallback.
- A second eval where the user creates a skill for the app before changing the app, to exercise criterion 1 end to end (`--dwt-ref` on `minds-evals generate` pins the branch without a config edit).
- The toy worktree and its five branches can be deleted once the numbers are no longer needed; the uncommitted eval config in `/Users/markally/imbue/wt-mngr-app-manifest-scoping/apps/minds_evals/configs/` can be deleted too.

## References

- Plan: `docs/system/blueprint/app-manifest-scoping/plan-app-manifest-scoping.md`.
- Commits: `19be2c213`, `1c90bee0d`, `494601659` on `mark/app-manifest-scoping`; toy fixture `a327acd99` (base), `1ae31fbb6` (rename), `8f5b6ef80` (reference removed) in the toy worktree.
- Arm prompts, runner, counter, and the six result JSONs: `/Users/markally/imbue/wt-app-manifest-scoping-toy/data/.tasks/harden/toy/arms/` (`run_arm.sh`, `count_reads.py`, `table.py`, `verify_*.txt`, `autofix_*.txt`, the six `*.json` results).
- Worker report from the eval: `agent/verification/workers/crystallize-todo/reports/consumed/done.md` under the trial directory above; the lead's task file is in `agent/snapshots/post_message_4.tar.gz` at `workspace/data/.tasks/harden/crystallize-todo/task.md`.
- Worker capture: `apps/minds_evals/imbue/minds_evals/evidence_collection.py` (`worker_capture_command`), `libs/mngr_claude/imbue/mngr_claude/plugin.py` (`preserve_sessions_on_destroy`, `on_destroy`) in mngr-internal.
