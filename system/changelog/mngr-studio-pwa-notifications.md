The lockfile adds `pywebpush` (with `py-vapid` and `http-ece`), which the shell uses to sign and encrypt the notifications it pushes to the owner's devices.

The desktop interface contracts (`docs/system/blueprint/desktop-interface/contracts.md`) describe the shell's Web Push state (`web_push/`, section 4.5b) and its routes: the notifications page and service worker, the signing key, the owner's devices, and the loopback-only `POST /api/web-push/send`.
