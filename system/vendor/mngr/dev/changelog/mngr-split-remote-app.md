Added `specs/split_remote_service_connector_app.md`: the design doc that established (and validated with a prototype) the Modal deployment mechanism for breaking apart remote_service_connector's single-file app.py, now updated with the implementation record.

Root config: added an import-linter layers contract for `imbue.remote_service_connector` (and its root package registration), and the `--cov=imbue.modal_app_kit` flag for the new shared library.
