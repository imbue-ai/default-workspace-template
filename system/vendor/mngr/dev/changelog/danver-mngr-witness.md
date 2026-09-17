Add `specs/behaviors-mapreduce/spec.md`, the design for re-homing the behaviors witness pipeline (`mngr tmr-behaviors`) into its own package on top of `mngr_mapreduce`'s agent-launching primitives, with a setup stage, a per-mapper adversarial review stage, mechanical gates for the mapper rules, a positively defined `FULL` verdict backed by a clause trace, provider-agnostic execution on local, docker, and modal, manifest-based resume, and a validation plan against the `libs/mngr_forward` corpus.

Record three documentation-versus-code conflicts noticed while writing it in `uncertainties.md`.

The spec's pipeline diagram is now generated: `specs/behaviors-mapreduce/pipeline.svg` is rendered from the declarative `WITNESS_PIPELINE` model in `libs/mngr_witness` by `mngr_mapreduce`'s generic `scripts/render_pipeline_svg.py`, and the spec embeds that image in place of the hand-drawn text diagram, so the picture cannot drift from the model (a test in `libs/mngr_witness` checks it). The root `pyproject.toml` gains the new package's `--cov` flag.

Tighten two repo-level ratchets in `test_meta_ratchets.py` (workspace vocabulary and minds references in mngr-level code) to the counts the tree now has.

Retire the TMR behaviors pipeline: the `tmr-behaviors` skill is replaced by `.agents/skills/witness/SKILL.md`, the runbook for `mngr witness`; `blueprint/tmr-behaviors/` is removed; `private.just`'s `tmr-behaviors-minds` recipe becomes `witness-minds`; `scripts/make_cli_docs.py` no longer lists the command; and the two `uncertainties.md` entries about the incipit and the old command's docs are resolved.

- The agent capability matrix script excludes the plugin-provided `witness-claude` type from the matrix, as it does for the other parent-class variants; it is `claude` with unattended defaults.
