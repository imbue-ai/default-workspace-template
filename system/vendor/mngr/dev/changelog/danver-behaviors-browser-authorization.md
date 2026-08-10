Updated the root `uv.lock` for apps/minds's new dev-dependency on `imbue-mngr-behaviors` (the behavior-corpus tooling used by its `browser-authorization` corpus guard test).

Kept `imbue-mngr-behaviors` out of the public mirror: `mirror/copy.bara.sky` excludes the corpus guard test (`apps/minds/imbue/minds/test_behavior_corpus.py`), and the dev-group and `uv.sources` references to that internal-only tooling in `apps/minds/pyproject.toml` are fenced with the mirror's internal-only markers so copybara strips them from the mirrored tree (which does not contain `libs/mngr_behaviors`).
