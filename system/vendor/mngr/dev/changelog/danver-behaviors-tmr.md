Add the `tmr-behaviors-minds` recipe to `private.just`: the canonical invocation of the new `mngr tmr-behaviors` recipe for the minds behavior corpus (`--root apps/minds/behaviors --name tmr-behaviors-minds --mapper-prompt apps/minds/tmr/behaviors_mapper.j2`, no testing flags after `--` since behavior mappers run their touched witness tests by node id).

Register `tmr-behaviors` in `scripts/make_cli_docs.py` so its docs page is generated.

Add the blueprint for the feature at `blueprint/tmr-behaviors/plan-tmr-behaviors.md` (de-complected during design; the simplification memo leads the file).

Update the root `uv.lock` for libs/mngr_tmr's new dependency on `imbue-mngr-behaviors`.
