"""Disk space. The memory figures come from ``oom_priority.meminfo``, where the resource sampler reads them too."""

import shutil
from pathlib import Path


def disk(path: Path) -> dict[str, int]:
    usage = shutil.disk_usage(path)
    return {"total": usage.total, "used": usage.used, "free": usage.free}
