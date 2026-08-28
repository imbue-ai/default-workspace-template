# Lock `concurrency-group` as a test dependency of `remote_service_connector`

`uv.lock` now records `concurrency-group` in the connector's `dev` dependency group, used by the box-script tests that run the rendered restore scripts under real bash. No shipped dependency changes.
