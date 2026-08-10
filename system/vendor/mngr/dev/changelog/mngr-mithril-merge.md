Merge main into the mithril-refactor stack: the root justfile now carries both the share-relay operational recipes (from this branch) and the `export-image-requirements` recipe (from main), and the workspace lock is regenerated with the connector's new ACME/JWT image pins.

The root `remote_service_connector layers contract` (import-linter) is rewritten for the merged module set: the deleted `tunnels`/`forwarding`/`naming` layers are dropped and the new `shares`/`share_certs`/`share_broker` modules are placed in the layer graph, so `lint-imports` passes again.
