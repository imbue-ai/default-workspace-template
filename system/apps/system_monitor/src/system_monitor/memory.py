"""Disk space as the workspace can use it (``disk_headroom``, which also sees a cloud machine's disk quota).

The memory figures come from ``oom_priority.meminfo``, where the resource sampler reads them too."""

from pathlib import Path

from disk_headroom.space import read_disk_space


def disk(path: Path) -> dict[str, int]:
    space = read_disk_space(path)
    return {"total": space.total, "used": space.used, "free": space.free}
