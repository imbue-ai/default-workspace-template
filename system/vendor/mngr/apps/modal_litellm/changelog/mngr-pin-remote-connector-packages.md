The LiteLLM proxy's Modal image is now fully pinned. The pip set (including the deliberately-pinned `litellm[proxy]==1.93.0`) moved into an `==`-pinned `[dependency-groups] image` in pyproject.toml, resolved in the workspace `uv.lock` and installed from the committed hash-locked `image_requirements.txt` export with `--require-hashes`. `prisma`, `pyyaml`, and `tenacity` are now pinned as well (previously unpinned at build time).

The workspace litellm moved 1.83.0 -> 1.93.0 to match what the deployed proxy already runs, so the pricing/config drift tests now exercise the production litellm version; a new drift test keeps the workspace and image litellm versions in lockstep.

The base image moved from `debian_slim(python_version="3.12")` to the digest-pinned `python:3.12-slim-trixie` shared via `modal_app_kit`, and the in-build uv version is pinned. Known residual gap: `prisma generate` still fetches version-determined (but not hash-verified) engine binaries from Prisma's CDN.
