The persona evals now grade what the agent **built**, not only how it talked about it. An agent that
chats beautifully and ships nothing no longer outscores one that ships a working app tersely.

A persona case may declare an `expectations` block in the eval config: `outcome` prose (the task
description *for the eval*, alongside the prompts *for the agent*) and a `deliverable` kind. The one
kind so far, `minds-app`, implies the standard shape of a delivered Minds app -- at least one
non-builtin app registered in the workspace's app registry, its supervisord service running, an HTTP
200 from each registered app's root path, and the delivered repo captured as a git bundle -- with
optional `min_registered_apps` / `http` / `files` entries refining that set rather than replacing it.
`test_commands` are run and recorded for the judge but never gated. `ui_flows` and `fresh_env` are
reserved: parsed and carried, but nothing executes them yet. Unknown kinds and unknown keys are
rejected at generation time rather than silently ignored.

The generator lowers the kind into its explicit check list exactly once and writes the lowered form
identically into `instruction.md` and `tests/case.json`, so the trial-time collector can never probe
a different set of checks than the grade-time judge scores.

After the last turn and while the workspace is still alive, the driver collects evidence into
`/logs/agent/verification/` (a new directory artifact): the app registry, `supervisorctl status`, a
file inventory, HTTP probes of each delivered app, any declared test commands, the repo's HEAD and
working-tree state, and an incremental `git bundle` of the agent's own commits. It is written
incrementally, so a phase that crashes still leaves what it gathered, and `trace.jsonl` records every
command it ran so a failure can be attributed to the app rather than to the instrument. The cheap
registry/service/inventory capture runs for *every* trial that reached a workspace, including cases
with no expectations, which is what makes a ships-nothing trial diagnosable after the fact.

Every recorded entry is `passed`, `failed` (the workspace fell short), or `error` (the harness could
not find out). `error` entries are excluded from the criteria they would feed, so an agent is never
charged for a broken measuring instrument.

Grading gains a third rewardkit dimension, `tests/outcome/`, emitted only for expectation cases: one
programmatic criterion per declared class (`app_registered`, `http_expectations_met`,
`files_expectations_met`) plus a `works_as_expected` judge over the rendered expectations, the
evidence manifest, and the conversation. For those cases `reward = gates_all_passed ? (0.5 * quality
+ 0.5 * outcome) : 0`; cases without expectations grade exactly as before. An expectations case whose
conversation finished but produced no evidence bundle now errors the trial rather than scoring a
misleading 0.

Two knobs: `verification_timeout_seconds` (config-level, default 600) budgets the collection phase
and is added to the task's `[agent].timeout_sec`, so verification never competes with the
conversation for time. `-a oracle` fabricates a green evidence bundle for expectation cases, so an
oracle run exercises the whole new path end to end.

The checked-in `eval-config-small.json` now declares expectations for the `todo-app` and
`landing-page` cases; `greeting` deliberately stays bare.

The failed/error line is drawn carefully in both directions. An app registry that exists and lists
nothing is the agent shipping nothing (`failed`), while one that is missing or unparseable is the
harness failing to look (`error`); `supervisorctl status` exits nonzero merely because a program is
down, so its output is recognized by content rather than exit code, and a missing `supervisorctl` or
a refused socket reads as `error` instead of a fleet of dead services. Probe output is parsed
first-marker-wins, so an app serving the harness's own section markers cannot forge a passing probe.

Two definitions turned out to need care, both grounded in the workspace template's own source. A
registry row is a *delivered* app only if it is not a builtin and not owned by a throwaway
"isolated instance" server -- those register through the same path and leave their row behind when
abandoned, so counting one would satisfy the app-registered check on something that was never the
deliverable while failing its root-path probe on a dead port. The rows are excluded by reading the
instance runner's own state file rather than by matching name patterns, since instance names are
supplied by whoever starts them. And a registry row is joined to its supervising program through the
`forward_port.py` calls in `system/supervisord.conf`, not by assuming row and program share a name:
a multi-port app registers extra origin rows that no program owns. A delivered row that no program
registers at all is recorded as a distinct shortfall (`no_supervised_program`) -- the app was started
by hand and would not survive a restart.

The eval-case commit the deliverable bundle is based on is now made with fixed author and committer
dates, so an identical tree always yields the same base sha. Without that the bundle could never be
unbundled onto a regenerated clone, which is the whole reason it is captured. The evidence records
both that base sha and the workspace-template tip it was built from.

`expectations` now requires a `deliverable`. A prose-only block would lower to no checks at all, and
rewardkit only pools a programmatic reward when criteria exist -- so the outcome dimension would
silently become judge-only, carrying double the judge weight of every other case and breaking the
cross-case comparability the fixed split exists for.

A live trial against a real workspace confirmed the collection path end to end and turned up one
more definition problem: the workspace registers infrastructure daemons (owner-exec, for one) in the
same app registry, marked `internal = true` -- "a port to forward but no page of its own to show".
Those answer 404 on `/` by design, so counting them as delivered apps both inflated the app count and
failed the implied root-path probe. Rows carrying that marker are now excluded, alongside builtins
and throwaway preview rows. The service-health join also falls back to a supervisord program named
exactly like the registry row, covering a service that registers its port at runtime rather than
through a `forward_port.py` call in the config.

Three more things a second review round pinned down. A trial where the agent committed nothing now
provably records `commit_count: 0` and no bundle as ordinary agent-side evidence -- `git bundle
create` refuses an empty range outright, so the guard has to sit before the call, and the
ships-nothing outcome this eval exists to catch must never be dressed up as the harness failing to
measure.

`reward-details.json` gains an `outcome_evidence` marker (the manifest's completeness bit, the entry
count, and per-class error counts) alongside the existing `timed_out` stamp. A class that lost some
entries to transient errors still scores over the survivors, and without the marker such a trial is
indistinguishable from a fully-measured one -- which would bias outcome scores upward and let a
bridge-reliability regression read as an agent improvement. The marker changes no score.

Reserved fields now fail loudly instead of silently: `fresh_env: true` and a `ui_flows` entry using
the reserved `script` field are rejected at generation time with a "known but unimplemented" error.
Accepting them handed a case author a green generation and a completed trial for verification that
never ran, which is the one failure mode a reserved field must not have. Natural-language
`steps` + `expect` flows are unaffected.
