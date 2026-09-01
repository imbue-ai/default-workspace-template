`system/vendor/mngr/` now carries only the **public subset** of the mngr monorepo -- byte for byte the same tree the Copybara mirror publishes to `imbue-ai/mngr` -- instead of a full copy of the private `imbue-ai/mngr-internal` repo.

This repo is public, and its contents are baked into every workspace we hand a user, so the private half of the monorepo should never have been here. 1,691 files are removed: the deployment/infra apps (`remote_service_connector`, `minds_admin`, `analytics`, `observability`, `share_relay`, `modal_litellm`, `oauth_redirector`, `apt_mirror`, `slack_exporter`), the internal-only libs (`mngr_tmr`, `mngr_mapreduce`, `mngr_claude_subagent_proxy`, `modal_app_kit`, `mngr_behaviors`), `litellm_proxy/`, `blueprint/`, `specs/`, `mirror/`, the operator runbooks under `apps/minds/docs/deploy/`, and the private CI workflows.

Nothing the workspace runs is affected: the root `uv.lock` is unchanged (identical resolution), every path dependency and every `pyproject.toml` the Dockerfile COPYs survives, and `mngr --version` plus the full plugin list are identical between an image built from the old tree and one built from this one.

The mngr-side change that produces this tree is `scripts/public_subset.py` (imbue-ai/mngr-internal). One dangling cross-reference to a now-absent vendored path was dropped from `docs/system/specs/rail-shortcuts-and-app-lifecycle.md`.
