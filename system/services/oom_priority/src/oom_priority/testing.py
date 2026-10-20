"""Shared helpers for the tests of the cgroup headroom publisher.

The policy (``cgroup_meminfo``) and the supervisord program that runs it
(``bin/publish_cgroup_meminfo.py``) are tested against the same fake cgroup v2
memory controller, laid out here so the two test files agree on it.
"""

from pathlib import Path


def write_fake_cgroup(
    cgroup_dir: Path, limit: str, usage: str, stat_lines: dict[str, int]
) -> None:
    """Lay out a cgroup v2 memory controller: ``memory.max``, ``memory.current`` and ``memory.stat``.

    ``limit`` and ``usage`` are the files' raw text (``"max"`` for an uncapped cgroup).
    """
    cgroup_dir.mkdir(parents=True, exist_ok=True)
    (cgroup_dir / "memory.max").write_text(f"{limit}\n")
    (cgroup_dir / "memory.current").write_text(f"{usage}\n")
    (cgroup_dir / "memory.stat").write_text(
        "".join(f"{key} {value}\n" for key, value in stat_lines.items())
    )
