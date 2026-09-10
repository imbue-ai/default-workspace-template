dev: fix the same wrong-Modal-JSON-key bug in two scripts.

`scripts/modal_nuke.py` and `scripts/changelog_schedule_utils.py` both read `modal app list --json` / `modal volume list --json` under the CLI's display column headings (`"App ID"`, `"Name"`, `"State"`) rather than the keys it emits (`app_id`, `name`, `state`). Both guarded that with their own schema check, so instead of silently doing nothing they raised on every row: `modal_nuke.py` refused to nuke anything, and `changelog_schedule_utils.py --stop-all-apps` (run by `changelog_deploy.sh` before every redeploy, to clear orphaned cron apps) failed instead of clearing them.

Both now parse through mngr's new `imbue.mngr.utils.modal_cli`, which raises `ModalCliOutputError` on an unexpected shape, and their bespoke `ModalSchemaError` / `_require_key` app and volume checks are gone. `changelog_schedule_utils.py` keeps its own tolerant environment-name lookup, whose casing genuinely has drifted across Modal versions.
