`serve_isolated_instance.py --copy` now checks a copy against what the workspace can actually still write (the `disk_headroom` library, which also reads a cloud machine's disk quota) and keeps 2 GiB of that free, instead of keeping 6 GiB of `statfs` free space to cover a quota it could not see. App previews no longer fail on a machine whose disk shows 4-6 GB free with room to spare, and still refuse when the quota is what is nearly full.

The caretaker and update-app skills check disk space with `uv run --no-sync disk-headroom` instead of `df`.
