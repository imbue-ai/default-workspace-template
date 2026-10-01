`forward_port.py` now takes the app's URL from its manifest, so `--url` is
optional with `--manifest`. Passing it still wins, which keeps a run that serves
somewhere else -- a test on an ephemeral port -- registering where it actually is.
This makes the manifest the declaration every static reader consults, which is
what lets the build-app port pre-flight and migrate-workspace's port scan account
for apps whose supervisord command names no port at all. chat, files and terminal
still hold the port as a constant in their own source as well; a new guard pins
the two together rather than letting them drift.

The browser's and the workspace shell's program commands no longer repeat the port
their manifest declares. `system/test_app_manifests.py` gains three guards: every
built-in app declares its port, no two declare the same one (nothing else catches
that -- registration upserts by name and never compares ports), and an app's
source names no port its manifest does not declare, so the constant and the
manifest cannot drift apart.
