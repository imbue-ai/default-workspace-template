The README's update-apply section points at update-self's `update_self_skill` package in `.agents/skills/update-self/python/` as the reference code, where the scripts-as-packages change moved it.

The README starts the backend from `.venv/bin/system-interface` rather than under `uv run`, following the rule that a long-running process never runs under `uv run`.
