The quality judge now grades the **progress timeline** -- the steps the agent declares with `tk create
--step` and closes with `tk close` -- as well as its chat messages. Both are copy the client reads, but
until now only the messages reached the judge, so an agent could write its entire plan in build
vocabulary and still score a clean `nontechnical_language`.

- `judge_transcript.txt` gains `[PROGRESS · step declared]` and `[PROGRESS · step done]` blocks, in
  the order the client saw them. They are recovered from `tk`'s own machine-readable output, already
  captured in the trajectory's observations, rather than from the shell command text -- which is what
  keeps regular cross-agent tickets, whose prose is not written for the client, out of the judged
  transcript.

- A new `nontechnical_status_language` likert criterion scores that timeline copy, and the existing
  `conciseness` and `nontechnical_language` criteria are now explicitly scoped to the chat messages.
  Keeping the two surfaces on separate criteria is what stops one paying for the other: folded into a
  single score, tidy step titles lift a run whose messages leak jargon, and jargon in the steps
  charges messages that were never at fault.

- The quality judge's weight moves 3.0 -> 4.0 so the dimension stays an equal-weight mean of all five
  criteria.

- A message's inline images now reach the judge as `[image: <alt text>]` rather than as raw markdown.
  The chat renders them as pictures, so the workspace path inside an image reference is markup the
  client never reads; shown the raw source, the judge scored it as a file path put in front of a
  non-technical client. The alt text stays, so the transcript still records that a picture was shown
  and what it was of.

The **wordiness guard is replaced by a message-length guard**. It scored the mean words per agent
turn against a baseline, which measured how the work divided across turns rather than how the agent
wrote: an agent that shows a mock-up before building delivers in its second turn and one that does
not delivers in its first, and the mean penalized the first for it even when both wrote the same
delivery message at the same length. The new criterion, `message_lengths_within_limits`, scores each
message against the limit for its role -- 300 words for the message that ends a turn, 30 for each
status message before it -- and reports the **fraction of turns** whose messages all keep their
limit, so one overlong turn in three costs a third rather than the whole criterion.

Which message ends a turn is read from the `finish_reason: end_turn` the workspace records, not from
position. A turn the trial cut short therefore has no closing message, and its last status line is
held to the 30-word limit instead of being allowed 300 on the strength of having been recorded last.
The driver's hand-built fallback trajectory marks no endings anywhere, and there a turn's single
merged message is its answer.

`avg_word_count_baseline` is no longer read at grade time. It is still accepted and recorded in each
case, so no config breaks, but it now configures nothing.

A new **`harness_quality`** dimension scores whether the workspace the agent was given actually let it
work, separately from how well the agent worked. A skill that will not load, a plugin the session
cannot resolve, a browser the tests cannot find: these look like agent failures in every other
dimension, because the agent visibly does not do the thing it said it would. Both todo-app runs we
measured hit exactly this -- `Unknown skill: imbue-code-guardian:autofix` in the hardening worker, so
neither run got the review gates it reported running.

- Two grade-time reports are rendered from the trial's own trajectories: `harness_main.txt` for the
  workspace agent the client talks to, and `harness_workers.txt` for every worker it launched. Both are
  processed rather than raw -- only the steps that invoked a skill, errored, or matched a known failure
  signature, with the agent's own words for that step, so a judge can tell a recovery from a surrender.

- Two likert-5 judges score them, `main_harness_success` and `worker_harness_success`, one per scope so
  neither is blamed for the other's failures.

- Two programmatic criteria, `main_harness_soundness` and `worker_harness_soundness`, score the counted
  failure signatures (`unknown_skill`, `missing_plugin`, `missing_command`, `missing_module`,
  `missing_browser`, `missing_path`) with no model in the loop. The score decays as
  `half_marks_at / (half_marks_at + count)` and never reaches zero: a trajectory samples what the
  harness did, and no finite count proves nothing worked. The per-signature breakdown is written to
  `harness_failures.json`.

`harness_quality` takes **a fifth of the reward**, applied to whatever the trial earned on quality and
outcome (so the parity between those two is untouched). The branch under test *is* the agent's
harness: a change made to help the agent can break the skills, plugins and tooling the whole system
runs on, and that regression is invisible everywhere else -- an agent whose review gate will not load
simply reads as an agent that chose to skip review. A trial graded before this dimension existed has
no such score and is composed exactly as it was then, so regrading an old capture does not silently
restate its reward.

Two corrections to how those criteria score, from review:

- A turn ends on any terminal stop reason, not only `end_turn`. A delivery message truncated at
  `max_tokens` ended its turn too; reading it as a status line held a long answer to the interim limit
  and failed the turn for being long, which inverts what the guard is for.

- A path the agent probed for and did not find is no longer counted against `harness_quality`. It stays
  in the report the judge reads, because it is context, and out of the scored total, because an agent
  that explores more is not an agent whose workspace is more broken.

Snapshots now also carry `/root/.mngr/preserved/`. When a lead merges a worker's branch and then
destroys the worker -- which the launch-task flow explicitly invites -- mngr rescues the worker's
transcripts there, in the agent side's own host dir, outside the workspace home tree the snapshot
took. The branch and the report survived; the conversation did not, so a hardening pass that ran and
reported was unreconstructable after the fact.

Every criterion is now framed so that a higher number is a better outcome, and named for the good
thing rather than the bad one: the gate that was `not_timed_out` is `finished_within_time`, and the
harness criteria that were `*_harness_failures` are `*_harness_soundness`. The values were already
oriented this way -- only the names read backwards, which is the kind of thing that gets misread in a
results table months later. What the harness *detects* is still named for the failure it is looking
for (`FAILURE_SIGNATURES`, `harness_failures.json`); that names an event, not a score.

**Scores are not comparable across this change**: it adds a criterion and rewrites the judge prompt,
so quality figures either side of it are different measurements. A trial whose agent declared no steps
scores `nontechnical_status_language` 10.

A failure signature is now only read out of tool output that came from *running* something, plus any
errored result. A file the agent read and a subagent's written report both routinely quote the exact
strings a broken harness prints -- a reference doc saying "do not `playwright install`", a review
agent describing the browser tests it found -- and scanning them counted documentation *about* a
failure as the failure. On the runs we measured this was every counted worker failure in one trial:
its worker harness scored 0.571 on three phantom hits while the judge reading the same steps scored
it a clean 5. Relatedly, a bare `playwright install` is no longer a `missing_browser` signature; it is
the remedy, and Playwright's own `Executable doesn't exist at .../ms-playwright/...` is what actually
identifies the failure.

A shell's `Exit code 127` now counts as a `missing_command` signature on its own. The criterion
previously keyed only on the shell's complaint (`ss: command not found`), which `2>/dev/null` removes
while leaving the exit status, so two agents that hit the same absent binary scored differently
depending on whether one redirected the complaint away -- the criterion was reading the agent's stderr
handling rather than the workspace. On the harden runs this hid a real gap: two leads both found `ss`
missing, and only the one that let the message through was counted.

The message-length guard's mechanics now live in `quality/message_lengths.py`'s module docstring
rather than in the top-level README, which keeps its config reference to what a config author needs:
`avg_word_count_baseline` is accepted so older configs still load and nothing reads it. The key is
gone from the README's example config, so it is no longer copied into new ones.

The README's two new sections on the progress timeline and the harness reports are gone, and the
`quality` and `harness_quality` bullets are back to naming what each criterion grades. Both sections
restated what `render_judge_transcript.py`, `render_harness_report.py` and `harness_quality/checks.py`
already say in their module docstrings, where the explanation stays true when the code moves; the
README keeps the reward math, which spans dimensions, and now names the module to read for each
criterion's own rules. The grade-time artifact list gains the harness reports alongside
`judge_transcript.txt`.

One thing the README was carrying alone is now pinned by a test rather than by prose: the judged
transcript walks only the top-level steps, never the `subagent_trajectories` embedded in them, so a
subagent's step titles cannot be graded as the client's progress timeline.

Still open, and not gated: a trial whose agent declared no progress steps scores
`nontechnical_status_language` 10, which pays an agent the same for good copy and for silence. Whether
a case warranted a timeline at all belongs in the structural gates rather than in a judge criterion.

Three fixes from review, all of them ways the new criteria measured the wrong thing:

The progress timeline is recovered from any step-record id, not only ids beginning `wor-`. `tk` derives
that prefix from the working directory's name, so a run rooted anywhere but a directory called
`workspace` had every step *declaration* dropped while its closes still rendered. A trial where the
parsing breaks scores `nontechnical_status_language` 10, so the failure was silent and upward.

The worker harness report is clipped as a whole, not only per worker. Each worker's share has a floor,
so enough workers overran rewardkit's judge-file cap -- and rewardkit does not error there, it hands
the judge `[skipped: file too large]`, which would leave the worker judge grading nothing while the
scripted criterion still counted every worker's signatures. Relatedly, the clip helper now counts its
own "N more characters" notice against the limit rather than appending past it.

`Exit code: 127` counts as a missing command alongside the bare `Exit code 127`. Both are the same
fact in two renderings; the colon form is what mngr's own CLI prints.

Four corrections to what the criteria treat as evidence, none of which moves any run we have captured:

`missing_browser` no longer claims any absent path whose name happens to contain a browser's. It
sorted ahead of the uncounted `missing_path`, so an agent's own `chrome_profile` directory scored as
breakage -- the very thing `missing_path` is uncounted to avoid. Playwright's own diagnostics and the
`ms-playwright` cache itself still count: absent, there is no browser to drive.

`missing_command`'s bare `not found` arm now requires the shell's own prefix (`sh: 1: ss: not found`,
which is what dash prints where bash says "command not found"). It used to match any
`<token>: not found` line, so ordinary key/value output read as a missing binary.

`missing_module` knows Node's errors as well as Python's. These evals commission browser apps, so a
missing npm dependency is as much a harness failure as a missing wheel, and was invisible.

The progress timeline is recovered only from what a shell tool printed, matching the rule the harness
report already followed. Every other tool returns content the agent asked *for* -- a file it read, a
subagent's prose -- which can quote another run's step records, and grading those charges the agent
for words it never wrote.

Three more corrections from review.

The judged transcript recovers a progress step from every line that first names its title, not only
from `Created <id>: <title>`. `tk start` prints `tk-step <id> title:`, and the client sees that title
on the timeline whether or not the step is ever closed -- so a step the agent opened and left open
used to render nothing at all. The title a `S1=$(tk create --step "...")` captured into a shell
variable is now recovered from the command text as well, which is the form the workspace's own gate
blesses; the zip onto output ids is refused unless the counts match exactly, so a ticket created in
the same breath drops the pairing rather than mislabelling the timeline with engineer-facing prose.

The message-length guard tells its two markerless shapes apart by whether the document stamps a
finish reason at all, rather than by whether any turn ended terminally. A trial cut short before it
ever finished a turn records only `tool_use`, so the old test called the workspace's own document
markerless and fell back to position -- handing 300 words to the last status line of every turn,
which is the case the marker exists to catch.

`finalize.py` reads every dimension the same way, absent meaning zero. Its compat branch for trials
predating `harness_quality` could not fire: the rewards it composes are the ones rewardkit just
produced, not the ones stored when the trial was captured, so a regrade grades an old trial on the
current verifier's dimensions and restates its reward. Where the branch *could* fire -- rewardkit
failing to emit the dimension -- it silently forgave a worse trial, while a missing `quality` scored
zero. The README said otherwise and has been corrected.

A structural gate, `progress_timeline_was_read`, now separates "the agent declared no steps" from
"the renderer failed to read the steps it declared". Both render an empty timeline and the judge
scores an empty timeline 10, so a parser that stops reading `tk`'s output used to earn a perfect mark
for copy nobody graded. The renderer writes `progress_summary.json` alongside the judge transcript
recording what it recovered and whether any `tk` step verb ran; the gate fails only when a step verb
ran and nothing was recovered. `tk`'s output format lives in another repo with no version pin here,
and has already broken this silently once.

`-a oracle` now carries a shell inference with a `tk` step record, so both grade-time pre-steps have
real output to read. Without it the oracle exercised neither: its timeline rendered nothing and its
harness report found nothing, so it scored a free 10 and a clean 1.0 on criteria whose code never ran.
