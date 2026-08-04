The connector's Modal image is now fully pinned. The pip set moved from unpinned names in `deploy_constants.py` to an `==`-pinned `[dependency-groups] image` in pyproject.toml, resolved in the workspace `uv.lock` (so unit tests run the same versions the container ships) and installed from the committed hash-locked `image_requirements.txt` export with `--require-hashes`.

The base image moved from Modal's `debian_slim()` to the digest-pinned `python:3.12-slim-trixie` shared via `modal_app_kit`, and the in-build uv version is pinned too.

New drift tests fail when the committed export no longer matches `uv.lock`, when an image group entry loses its `==` pin, or when the group and `THIRD_PARTY_IMPORT_ROOTS` disagree. Regenerate exports with `just export-image-requirements`.

Note: the first deploy after this change bumps the container's package versions to the current `uv.lock` resolution (e.g. fastapi 0.139.2).
