Declares the port it serves in its `app.toml` as `url`. Registration reads it from
there, so the port is written in one place and tooling that never starts the app --
the build-app port pre-flight, migrate-workspace's port reconciliation -- can
account for the port this app holds instead of handing it to a new one.
