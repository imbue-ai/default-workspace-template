- `system/scripts/build_auth_sandbox_image.py` builds the web latchkey auth sandbox's Modal image from the product `system/Dockerfile`: a workspace's first-boot seed and the Screen unit, then the sign-in pieces from the new `system/libs/auth_sandbox` library, snapshotted and printed as an image id for the tier's `MINDS_AUTH_SANDBOX_IMAGE_ID`.

- `pyproject.toml` pins `imbue-mngr-latchkey` and `imbue-mngr-forward` from the same mngr commit as every other mngr package, for the auth sandbox library's use of the desktop's sign-in wrapper.

- `build_auth_sandbox_image.py` no longer passes `environment_name` to `Sandbox.create` (deprecated by Modal; the app the sandbox belongs to already names the environment).
