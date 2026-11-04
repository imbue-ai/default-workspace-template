#!/usr/bin/env python3
"""Supervisord program: record the workspace's CPU and memory for the system monitor.

Every few seconds this walks ``/proc`` once, works out how busy the CPU was since the
last walk and how much memory is in use, rewrites the recent file the system monitor
app's graphs read, appends a line per closed minute to the day of history, and records
a busy stretch (CPU above 90% for at least two minutes) with the chats and programs that
caused it (``oom_priority.resource_sampler`` has the policy;
``docs/system/specs/system-monitor.md`` the design).

Logs only when something changes: the first reading, a stretch opening or closing, a
walk that starts failing or recovers.

Self-contained beyond the stdlib-only ``oom_priority`` package (imported via a
``sys.path`` insert), since supervisord runs this under a plain ``python3``.
"""

import argparse
import logging
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oom_priority import resource_sampler, supervisor_rpc
from oom_priority.cgroup_meminfo import HOST_MEMINFO_PATH


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Record CPU and memory readings for the system monitor app."
    )
    parser.add_argument("--state-dir", type=Path, default=resource_sampler.STATE_DIR)
    parser.add_argument("--proc", type=Path, default=resource_sampler.PROC)
    parser.add_argument("--cgroup-dir", type=Path, default=resource_sampler.CGROUP_DIR)
    parser.add_argument("--host-meminfo", type=Path, default=HOST_MEMINFO_PATH)
    parser.add_argument(
        "--supervisor-socket", type=Path, default=supervisor_rpc.SUPERVISOR_SOCKET
    )
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=resource_sampler.SAMPLE_INTERVAL_SECONDS,
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=0,
        help="stop after this many rounds; 0 runs forever (tests)",
    )
    return parser.parse_args(argv)


class _Loop:
    """The loop's state: whether the last walk worked and whether a stretch is open, so only a change is logged."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.sampler = resource_sampler.Sampler(
            state_dir=args.state_dir,
            proc=args.proc,
            cgroup_dir=args.cgroup_dir,
            host_meminfo=args.host_meminfo,
            roots_reader=lambda: resource_sampler.read_roots(args.supervisor_socket),
        )
        self.rounds = 0
        self.was_failing = False
        self.was_open = False

    def run_round(self) -> bool:
        """One round; False once the loop should stop (only with ``--rounds``)."""
        self.rounds += 1
        try:
            event = self.sampler.sample_once()
        except OSError as error:
            if not self.was_failing:
                logging.warning("a reading failed, retrying each round: %s", error)
            self.was_failing = True
        else:
            if self.was_failing:
                logging.info("readings recovered")
            self.was_failing = False
            if self.rounds == 2:
                logging.info(
                    "recording CPU and memory readings to %s", self.args.state_dir
                )
            is_open = self.sampler.stretches.is_open
            if is_open and not self.was_open:
                logging.info("CPU busy stretch opened")
            if event is not None:
                logging.info(
                    "CPU busy stretch recorded: %s s, peak %s",
                    event["duration_seconds"],
                    event["peak"],
                )
            self.was_open = is_open
        return self.args.rounds <= 0 or self.rounds < self.args.rounds


def main() -> None:
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO, format="resource_sampler: %(message)s"
    )
    loop = _Loop(_parse_args(sys.argv[1:]))
    pause = threading.Event()
    while loop.run_round():
        pause.wait(loop.args.interval_seconds)


if __name__ == "__main__":
    main()
