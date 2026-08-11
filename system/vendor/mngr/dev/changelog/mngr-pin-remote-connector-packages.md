Pinned the Modal service images (plan in `blueprint/pin-modal-service-images/`): image pip sets are now `==`-pinned per-app `image` dependency groups resolved in the workspace `uv.lock` and installed from committed hash-locked exports; base images are digest-pinned; drift tests, a repo-wide ratchet, and a `minds env deploy` freshness preflight enforce the convention.

Added the `just export-image-requirements` recipe to regenerate the committed exports.

Advanced the root `[tool.uv] exclude-newer` supply-chain cooldown cutoff forward-only from 2026-06-04 to 2026-07-20 to admit `litellm==1.93.0` (published 2026-07-19, and already what the deployed proxy runs); the workspace relock bumps litellm 1.83.0 -> 1.93.0 and fastapi to 0.139.2 (litellm 1.93.0 requires fastapi>=0.136.3).

Advanced the public-mirror overlay's `exclude-newer` to the same 2026-07-20 cutoff and regenerated `mirror/overlay/uv.lock` (seeded from the private lock, per the mirror gate's `--lock` flow): the two cutoffs must move together, since a mismatch between the seeded lock's recorded timestamp and the overlay config forces uv into a full re-resolve and drifts the public lock. The public lock picks up the same cryptography/fastapi/mcp/pyjwt versions private CI tests.
