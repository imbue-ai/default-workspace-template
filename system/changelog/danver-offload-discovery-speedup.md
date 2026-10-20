# Speed up offload pytest discovery in CI (OFFLOAD-16, OFFLOAD-17, OFFLOAD-18)

Ports the mngr-internal discovery speedup (mngr-internal PR #1804) to this repo's `test-offload` job, which has the same shape: pytest collection runs on a cold GitHub runner while the Modal sandbox fleet waits.

- `test-offload` now runs `uv sync --all-packages --compile-bytecode`, moving site-packages bytecode compilation into uv's parallel install phase.

- The job precompiles repo bytecode (`python -m compileall system catalog`) so modules under `system/` are also warm before discovery.

- `discovery_args` in all three offload configs (`offload-modal.toml`, `offload-modal-chat.toml`, `offload-modal-system-interface.toml`) now pass `--assert=plain`, skipping pytest's assertion-rewriting of test modules during collection.
