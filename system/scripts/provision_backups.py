#!/usr/bin/env python3
"""Initialize this workspace's restic backup repository from its restic.env.

The hosted minds web client provisions backups entirely in the workspace: it
mints the bucket + S3 key against the connector, writes the canonical
``data/.secrets/restic.env`` (via the owner-exec write-file endpoint), then
runs this script (via owner-exec run) to create the restic repository. The
``host-backup`` service then takes over on its normal cadence.

Idempotent: ``restic init`` against an already-initialized repository reports
"already initialized" / "already exists", which this treats as success -- so a
re-run (a retried create, a re-provision) never fails.

Usage:
    python3 system/scripts/provision_backups.py [--env-file data/.secrets/restic.env]
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

_DEFAULT_ENV_FILE = Path("data/.secrets/restic.env")
_REQUIRED_KEYS = ("RESTIC_REPOSITORY", "RESTIC_PASSWORD")
_INIT_TIMEOUT_SECONDS = 120.0

# restic's own phrasing when the repository already exists; treated as success.
_ALREADY_INITIALIZED_MARKERS = (
    "already initialized",
    "already exists",
    "config file already exists",
)


class BackupProvisionError(RuntimeError):
    """Raised when the backup repository cannot be initialized."""


def parse_restic_env_file(content: str) -> dict[str, str]:
    """Parse a restic.env file body into a dict (mirrors host_backup's parser)."""
    values: dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, separator, value = line.partition("=")
        if not separator or not key.strip():
            continue
        cleaned_value = value.strip()
        if len(cleaned_value) >= 2 and cleaned_value[0] == cleaned_value[-1] and cleaned_value[0] in ("'", '"'):
            cleaned_value = cleaned_value[1:-1]
        values[key.strip()] = cleaned_value
    return values


def initialize_repository(env_values: dict[str, str]) -> bool:
    """Run ``restic init`` with ``env_values`` in the environment.

    Returns True when a fresh repository was created, False when it already
    existed. Raises :class:`BackupProvisionError` on a missing key or a real
    init failure.
    """
    missing = [key for key in _REQUIRED_KEYS if not env_values.get(key)]
    if missing:
        raise BackupProvisionError(f"restic.env is missing required keys: {', '.join(missing)}")
    result = subprocess.run(
        ["restic", "init"],
        env={**os.environ, **env_values},
        capture_output=True,
        text=True,
        timeout=_INIT_TIMEOUT_SECONDS,
    )
    if result.returncode == 0:
        return True
    combined_output = (result.stdout + result.stderr).lower()
    if any(marker in combined_output for marker in _ALREADY_INITIALIZED_MARKERS):
        return False
    raise BackupProvisionError(f"restic init failed (exit {result.returncode}): {result.stderr.strip()}")


def provision_from_file(env_file: Path) -> bool:
    if not env_file.is_file():
        raise BackupProvisionError(f"restic env file not found: {env_file}")
    return initialize_repository(parse_restic_env_file(env_file.read_text()))


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize the workspace restic repository.")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=_DEFAULT_ENV_FILE,
        help="Path to the restic.env file (default: data/.secrets/restic.env)",
    )
    arguments = parser.parse_args()
    try:
        was_created = provision_from_file(arguments.env_file)
    except BackupProvisionError as exc:
        sys.stderr.write(f"{exc}\n")
        raise SystemExit(1) from exc
    sys.stdout.write("repository initialized\n" if was_created else "repository already initialized\n")


if __name__ == "__main__":
    main()
