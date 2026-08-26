A trial whose `tests/case.json` is missing, unparseable, not a JSON object, or whose `expectations`
is neither an object nor `null` is now reported as a grading-infrastructure failure: `finalize.py`
leaves no reward file and harbor errors the trial, the same path as a failed judge call or
unmeasurable outcome evidence. The generator writes that file into every task, so a broken one is
the harness failing rather than the agent -- and reading it as "this case declared no expectations"
would grade a case that commissioned a deliverable at quality-only weight.

The check is unconditional: the case file is part of the task, not of the run, so it is verified
whether or not the trial's gates passed or it timed out. A valid case file with `expectations`
absent or `null` remains the bare case and grades quality-only, unchanged.
