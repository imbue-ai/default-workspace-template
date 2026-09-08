The generator step that turns a case's `deliverable.kind` shorthand into its explicit check list is
called *expansion*: `expand_expectations` produces an `ExpandedExpectations`, and the docstrings,
comments, and docs say "expanded" throughout. Nothing about the dataset format changes -- the
serialized `expectations` and `authored_expectations` keys are the same, so existing datasets and
captured trials regrade unchanged.

The trial-time evidence collector now lives in `imbue/minds_evals/evidence_collection.py` (was
`verification.py`), which leaves "verification" meaning just two things in this app: the
`verification/` evidence directory a trial writes, and the UI-flow verification agent. Both are
unchanged, as are `verification_timeout_seconds` and the `/logs/agent/verification/` artifact path.

The judge-digest renderer names its successively smaller per-step state budgets
`_EARLIER_STEP_STATE_THRESHOLDS`, and `outcome/checks.py` imports rewardkit as `rk`.
