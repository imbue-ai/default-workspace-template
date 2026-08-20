New just recipes for the bare-metal box lifecycle (issue #496 Phase 3): `just order-server` (pass `--dry-run` for the no-charge price preview), `just await-delivery <server-id>`, and `just setup-server <server-id>` -- all thin aliases over the env-aware `minds-admin server` commands.

`just prep-server` collapsed to a thin alias: the observability collector assembly (Vault credential resolution + script render + `--extra-prep-script` plumbing) moved in-process into `minds-admin server prep` / `setup`, so the recipe's `_derive_observability_tier` / `provision_observability_config.py collector-env` shell block is gone from the prep path. The relay recipes keep using the script; its docstring now notes the boxes-sender path is served by `minds-admin`.

The `minds-justfile` skill documents the new recipes.
