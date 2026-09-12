#!/usr/bin/env python3
"""Script to clean up old Modal test environments.

This script is run by a CI job (on every push to main and pull request) to clean
up Modal test environments that are older than a specified age. This helps prevent
accumulation of stale environments when test processes crash without proper cleanup.

Exits non-zero if any environment survived its delete, so a sweep that cannot
actually reap turns the CI job red instead of reporting success.

Usage:
    uv run python scripts/cleanup_old_modal_test_environments.py [--max-age-hours HOURS]
    uv run python scripts/cleanup_old_modal_test_environments.py --dry-run

Options:
    --max-age-hours  Maximum age in hours for environments to keep (default: 1.0)
    --dry-run        Print which environments the sweep would touch, and delete nothing
"""

import argparse
import sys
from collections.abc import Sequence
from datetime import timedelta
from typing import Final

from imbue.imbue_common.logging import setup_logging
from imbue.imbue_common.pure import pure
from imbue.mngr_modal.cleanup import ModalTestEnvironmentSweepResult
from imbue.mngr_modal.cleanup import cleanup_old_modal_test_environments
from imbue.mngr_modal.cleanup import find_old_test_environments

_NOTHING_TO_DO: Final[str] = "No old Modal test environments found to clean up"


@pure
def describe_planned_sweep(environment_names: Sequence[str]) -> str:
    """Return the summary for a --dry-run: the environments the sweep would touch."""
    if not environment_names:
        return _NOTHING_TO_DO
    return f"Would sweep {len(environment_names)} old Modal test environment(s):\n" + "\n".join(
        f"  {name}" for name in environment_names
    )


@pure
def report_sweep(result: ModalTestEnvironmentSweepResult) -> tuple[int, str]:
    """Return the process exit code and the human-facing summary for a sweep result."""
    if result.failed_environment_names:
        return 1, (
            f"Failed to delete {len(result.failed_environment_names)} old Modal test environment(s): "
            + ", ".join(result.failed_environment_names)
        )
    if result.deleted_environment_names:
        return 0, f"Cleaned up {len(result.deleted_environment_names)} old Modal test environment(s)"
    return 0, _NOTHING_TO_DO


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Clean up old Modal test environments",
    )
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=1.0,
        help="Maximum age in hours for environments to keep (default: 1.0)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print which environments the sweep would touch, and delete nothing",
    )
    args = parser.parse_args()

    setup_logging(level="INFO")

    # The rehearsal has to select environments the way the real sweep does, so it
    # shares find_old_test_environments and simply stops before touching anything.
    if args.dry_run:
        print(describe_planned_sweep(find_old_test_environments(timedelta(hours=args.max_age_hours))))
        return 0

    exit_code, summary = report_sweep(cleanup_old_modal_test_environments(max_age_hours=args.max_age_hours))
    print(summary)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
