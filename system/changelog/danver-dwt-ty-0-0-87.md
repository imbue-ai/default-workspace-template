`uv.lock` and the root project record `ty` 0.0.87 (was 0.0.85) for every project's type-check ratchet.

The root `pyproject.toml` carries the same two-week supply-chain cooldown as mngr-internal: `[tool.uv] exclude-newer = "14 days"`, so a relock only adopts a registry release once it has been public for two weeks, with `required-version = ">=0.10"` because an older uv drops a relative cutoff silently. The lock records the window (`exclude-newer-span = "P14D"`), and `types-pyyaml` moves back from 6.0.12.20261030 to 6.0.12.20260906, the newest release the window admits.
