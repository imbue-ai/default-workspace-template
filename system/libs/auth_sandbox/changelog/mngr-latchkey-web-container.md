- New library for the web latchkey auth sandbox: the commands owner-exec runs on the Imbue Studio web chrome's behalf (`auth-sandbox-sign-in`, `auth-sandbox-set-credentials`, `auth-sandbox-import-browser-state`, `auth-sandbox-export`), thin click entry points over `imbue.mngr_latchkey.core.Latchkey` (the desktop's sign-in sequence) and `latchkey auth re-encrypt`, with keys read from `/run/auth-sandbox/` and passed on stdin or in the environment, never in an argument.

- The sandbox's boot script (`sandbox/boot.sh`, installed at `/opt/auth-sandbox/boot.sh`) and supervisord configuration (the Screen service with its stack group and watcher, the share gateway, and owner-exec; nothing else), and the image build steps `system/scripts/build_auth_sandbox_image.py` runs in a Modal sandbox before snapshotting it.

- The template `config.json` the image carries: minds' hidden built-ins and registered services plus a browser entry naming the Fortress launcher, rendered by `auth-sandbox-render-config` at image build and verified with `latchkey ensure-browser --source existing-config`.

- The image build's latchkey version check runs the CLI under a throwaway directory and key, as the ensure-browser step already did: `latchkey --version` refuses to start without an encryption key, and a sandbox has no system keychain to mint one from, so the check failed on the first real build.
