Migrated the eval box's `write_file` off Modal's legacy Sandbox filesystem API. Modal drops server-side support for `sandbox.open()` on 2026-09-14 -- after which the call errors out even on clients where the method still exists -- so `write_file` now uses `sandbox.filesystem.write_text(content, path)` instead.

Also raised the `modal` dependency floor from `>=1.0` to `>=1.4.3`, matching the workspace-wide `modal==1.4.3` pin and the repo's other modal consumers, so the resolver can no longer pick a client that lacks the `sandbox.filesystem` API (introduced in 1.4.0).
