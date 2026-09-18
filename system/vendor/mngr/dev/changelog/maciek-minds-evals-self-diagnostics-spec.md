`specs/minds-evals-self-diagnostics/spec.md` designs the minds_evals self-diagnostic suite (#934), which validates the eval instrument rather than the agent, in two families of nightly cases, so a regression in the instrument is red on the night it lands instead of misreporting that night's real trials or drifting into judge scores.

- Every assertion is made at check time by `minds-evals check-diagnostics`, from the job directory harbor collected, so a fact can only be right if the record the graders read is right. The driver persists records and computes nothing for the suite; the checker also reads any past job directory in a record-only mode, and a small live-invariants table is read on every nightly cell.

- The fixture family boots a real workspace from a seeded commit built in the box before launch (authored on the template, merged onto the case base and checked for collisions, so a seed that cannot apply ends the trial as impossible), drives every evidence check class against a fixed app with scripted UI flows only (including one that addresses a control with no accessible name by the text beside it), includes one deliberately failing check per class so a reader that passes everything is red, and asserts exact known answers: one reading per flow that only its knob can produce, the structural gates by name, the bundle verified and the snapshot readable.

- The behaviour family prompts a cheap agent on each harness the nightly configs use (claude, pi-coding, codex), in the configuration a nightly cell of that lane gets, to track steps with `tk`, run tools, fail a command on purpose, launch a background worker and read an upload across two steps, and asserts that the tickets, the event feed, the captured trajectory, the verifier's derived outputs, the agent listing and the step records all agree. Compliance facts say what the agent did, health facts say whether their sources could be read, and a source that could not be read is an instrument failure, never "the agent did not comply".

- Shared machinery: expected tables bound to a case id, with per-harness overrides, strict known failures and compliance requirements; verdicts passed, known, failed, not followed and not measured, with a not-recorded status for a record the instrument did not write; cells a pair cannot run are left out of the matrix rather than excused in a table; diagnose jobs per pair and per pair and harness report into the nightly Slack message, and two consecutive nights without a measurement are named as coverage lost.

- The in-box proxy is out of the suite's scope: the nightly's live cells run without it. `proxy_calibration_notes.md` and `update_path_notes.md` keep the first cut's findings on proxy calibration and on landing a change through `update_self.py apply` for the follow-ups that need them.

`specs/minds-evals-self-diagnostics/uncertainties.md` records where the code contradicts the issues or the template's docs: no per-flow path field exists for the fixture's knobs, a seed's pre-existing classification depends on when it lands, and the build-app skill's escape-hatch app shape breaks the template's uv workspace.

`just test-minds-evals` with no args runs the minds_evals suite as two pytest sessions across two xdist workers each: the real-browser `chromium` tests in one, everything else in the other, with coverage appended across both. Each session gets its own 150 s CI budget, so the suite fits CI's runner with room to grow. With args (`just test-minds-evals <path>::<test>`, a `-m`, ...) it runs the one session they select.

- `specs/minds-eval-harbor/outcome_verification.md` says the deliverable bundle holds the commits made in the workspace: the template's first-boot commit, then the agent's.

`.github/workflows/minds-evals-scheduled.yml` runs two jobs beside `evaluate` on every live night: `diagnose-fixture`, one per resolved pair, and `diagnose-behaviour`, one per resolved pair and supported nightly harness. They depend on `resolve` alone, so neither the oracle nor a green marker skips them, and they gate nothing. Each generates its family's case at the pair's frozen SHAs, runs it, checks it with `minds-evals check-diagnostics` (the job fails only on a `failed` trial), deletes the environments it recorded, and uploads `minds-evals-summary-<pair>-diagnose-fixture` or `minds-evals-summary-<pair>-diagnose-behaviour-<harness>` and its job directory; `diagnose-fixture` also runs the age-based backstop sweep.

`evaluate` reads every live cell against `configs/diagnostics/live_invariants.json` in the same step as `check-run` and uploads the result beside it. The read gates nothing: a miss is named in the Slack report's details and never in the cell's verdict.

`notify` waits for both diagnose jobs and passes their results to `ci-report`.

The `evaluate` job's live-invariants summary carries the eval config's slug beside the pair and the arm, the way its `check-run` summary does, so two suites' cells of one arm cannot overwrite each other's reading.

