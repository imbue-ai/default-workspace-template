The minds-eval outcome-verification spec's failure semantics now list the case file alongside the
evidence rules: a `case.json` that is missing, unparseable, not a JSON object, or whose
`expectations` is neither an object nor `null` is a grading-infrastructure failure, checked
unconditionally because the case file belongs to the task rather than to the run.
