#!/usr/bin/env python3
"""Supervisord program: publish the container's own memory headroom for earlyoom.

Under runc the container's ``/proc/meminfo`` is the host kernel's, so earlyoom
(``system/supervisord.conf.d/earlyoom.conf``) would shed at 10% of the whole
machine while the container's cgroup OOM killer acts at the workspace's cap.
This loop reads the container's cgroup v2 memory controller four times a second
and writes its limit and headroom to the file earlyoom reads with
``--host-meminfo`` (``oom_priority.cgroup_meminfo`` has the formula and the
cases where it has no answer and leaves the file alone). Under gVisor it has
nothing to add (a gen-2 slice VM publishes the file from outside), so it says
so and exits 0, which the program's ``exitcodes`` treat as the end of its job.
Everywhere else it never exits on its own: a cap can arrive at runtime
(``mngr docker resize`` on an uncapped workspace), so an uncapped container is
re-checked every round rather than abandoned.

Logs only when the outcome of a round changes, so a steady state is silent.

Self-contained beyond the stdlib-only ``oom_priority`` package (imported via a
``sys.path`` insert), since supervisord runs this under a plain ``python3``.
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oom_priority import cgroup_meminfo

_OUTCOME_DESCRIPTIONS = {
    cgroup_meminfo.PublishOutcome.PUBLISHED: "publishing the cgroup's limit and headroom",
    cgroup_meminfo.PublishOutcome.UNCAPPED: "no memory cap on this cgroup; nothing published",
    cgroup_meminfo.PublishOutcome.NO_CONTROLLER: "no cgroup v2 memory controller here; nothing published",
    cgroup_meminfo.PublishOutcome.UNREADABLE: "the cgroup's memory counters cannot be read; nothing published",
    cgroup_meminfo.PublishOutcome.GVISOR: "running under gVisor; the host publishes the file, exiting",
}


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Publish the container's cgroup memory headroom for earlyoom's --host-meminfo."
    )
    parser.add_argument("--cgroup-dir", type=Path, default=cgroup_meminfo.CGROUP_DIR)
    parser.add_argument("--target", type=Path, default=cgroup_meminfo.HOST_MEMINFO_PATH)
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=cgroup_meminfo.PUBLISH_INTERVAL_SECONDS,
    )
    parser.add_argument(
        "--once", action="store_true", help="one round, then exit (tests)"
    )
    # The running kernel decides the gVisor case; tests name a release of their
    # own, since the CI sandboxes themselves run under gVisor.
    parser.add_argument("--kernel-release", default=os.uname().release)
    return parser.parse_args(argv)


class _Publisher:
    """The loop's state: what the last round did, so only a change is logged."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.last_outcome: cgroup_meminfo.PublishOutcome | None = None

    def run_round(self) -> bool:
        """One round; False once the loop should stop (only in ``--once`` mode)."""
        outcome = cgroup_meminfo.publish_once(
            self.args.cgroup_dir,
            self.args.target,
            self.args.kernel_release,
            int(time.time()),
        )
        if outcome != self.last_outcome:
            logging.info("%s (%s)", _OUTCOME_DESCRIPTIONS[outcome], self.args.target)
            self.last_outcome = outcome
        return (
            not self.args.once and outcome is not cgroup_meminfo.PublishOutcome.GVISOR
        )


def main() -> None:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="publish_cgroup_meminfo: %(message)s",
    )
    publisher = _Publisher(_parse_args(sys.argv[1:]))
    while publisher.run_round():
        time.sleep(publisher.args.interval_seconds)


if __name__ == "__main__":
    main()
