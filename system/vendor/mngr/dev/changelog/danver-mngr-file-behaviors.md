Excluded `libs/mngr_file/imbue/mngr_file/test_behavior_corpus.py` from the public mirror, alongside
the equivalent guard tests already excluded for `apps/minds` and `libs/mngr_forward`. `mngr_file`
gained a behavior corpus, and the test guarding it dev-depends on `imbue-mngr-behaviors`, which is
internal-only and absent from the public tree; that dev-group is stripped from the mirrored
`libs/mngr_file/pyproject.toml` via `BEGIN-INTERNAL` markers, so the test cannot run publicly. The
corpus itself is mirrored as normal.
