`serve_isolated_instance.py --copy` now checks a copy against what the workspace can actually still write (the `disk_headroom` library, which also reads a cloud machine's disk quota) and keeps 2 GiB of that free, instead of keeping 6 GiB of `statfs` free space to cover a quota it could not see. App previews no longer fail on a machine whose disk shows 4-6 GB free with room to spare, and still refuse when the quota is what is nearly full.

`serve_isolated_instance.py` moves to the venv tier, since it now imports a venv-tier library: run it with `uv run --no-sync`, not `python3` (update-app's commands and the script's own printed hints say so). `running-python.md` says which bare code has to stay bare, and that other stdlib-only code moves to the venv tier when it needs something not already bare.

The caretaker and update-app skills check disk space with `uv run --no-sync disk-headroom` instead of `df`.
