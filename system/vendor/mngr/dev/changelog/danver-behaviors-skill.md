Added the `behaviors` skill (`.claude/skills/behaviors/SKILL.md`): the definitional reference for the behavior language - Gherkin `.feature` files in per-project corpora at `<project>/behaviors/`, validity via `gherkin-official`, kebab-case folder/file/tag naming, first-tag identity with folder-derived coordinates unique per corpus, invariants as `Rule:` blocks with folder scoping bounded by the corpus (reserved `invariants.feature` and `overview.md` basenames), the `witnesses` test back-link convention with its corpus/test-tree pairing rule, and the `mngr behaviors` CLI.

The `behaviors` skill owns the canonical one-sentence-per-line rule for corpus prose (its "Prose style" section: sentences in the same paragraph follow one another immediately with no blank lines, and paragraphs are separated by a single blank line).
The `writing-specs` skill references it rather than restating it.

Root `pyproject.toml` gains the `imbue.mngr_behaviors` coverage flag required by the meta-ratchet, and `scripts/make_cli_docs.py` adds `behaviors` to the generated secondary-command docs set.

Updated the root `uv.lock` for the new `libs/mngr_behaviors` library (which carries the `gherkin-official` dependency).
