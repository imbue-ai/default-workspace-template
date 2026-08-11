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
import time
from collections.abc import Callable
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

# A freshly-minted R2 / S3 credential takes a few seconds to become active at
# the storage backend's edge; a restic call against it before then fails with
# a transient auth error. The web-create path mints the bucket key and runs
# this init immediately, so without a retry it reliably loses that race. Retry
# the init for a bounded window on those signals only (mirrors the desktop's
# restic_cli auth-propagation retry). Genuine auth failures keep retrying until
# the deadline and then surface -- acceptable: a create's backup step is not
# hot-path latency-sensitive, and the alternative is a spurious hard failure.
_AUTH_PROPAGATION_RETRY_SECONDS = 60.0
_AUTH_PROPAGATION_WAIT_SECONDS = 3.0
# Both the raw S3 error codes and restic's rendered human phrasings: a
# just-minted key that has not propagated surfaces as InvalidAccessKeyId
# (edge does not know the key id yet) or a signature mismatch (edge knows the
# id but not the secret), and restic prints the latter as "The request
# signature we calculated does not match ..." rather than the bare code.
_TRANSIENT_AUTH_SIGNALS = (
    "unauthorized",
    "invalidaccesskeyid",
    "invalid access key",
    "signaturedoesnotmatch",
    "request signature we calculated does not match",
)


class _InitAttempt:
    """One ``restic init`` invocation's outcome (exit code + captured output)."""

    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _run_restic_init(env_values: dict[str, str]) -> _InitAttempt:
    result = subprocess.run(
        ["restic", "init"],
        env={**os.environ, **env_values},
        capture_output=True,
        text=True,
        timeout=_INIT_TIMEOUT_SECONDS,
    )
    return _InitAttempt(result.returncode, result.stdout, result.stderr)


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
        if (
            len(cleaned_value) >= 2
            and cleaned_value[0] == cleaned_value[-1]
            and cleaned_value[0] in ("'", '"')
        ):
            cleaned_value = cleaned_value[1:-1]
        values[key.strip()] = cleaned_value
    return values


def initialize_repository(
    env_values: dict[str, str],
    *,
    # Seams so the retry loop is testable without a real restic or real waits.
    run_init: Callable[[dict[str, str]], _InitAttempt] = _run_restic_init,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> bool:
    """Run ``restic init`` with ``env_values`` in the environment.

    Returns True when a fresh repository was created, False when it already
    existed. Retries on transient storage-auth errors (a just-minted key that
    has not propagated to the backend edge yet) for a bounded window. Raises
    :class:`BackupProvisionError` on a missing key or a real init failure.
    """
    missing = [key for key in _REQUIRED_KEYS if not env_values.get(key)]
    if missing:
        raise BackupProvisionError(
            f"restic.env is missing required keys: {', '.join(missing)}"
        )
    deadline = monotonic() + _AUTH_PROPAGATION_RETRY_SECONDS
    while True:
        attempt = run_init(env_values)
        if attempt.returncode == 0:
            return True
        combined_output = (attempt.stdout + attempt.stderr).lower()
        if any(marker in combined_output for marker in _ALREADY_INITIALIZED_MARKERS):
            return False
        is_transient_auth = any(
            signal in combined_output for signal in _TRANSIENT_AUTH_SIGNALS
        )
        if is_transient_auth and monotonic() < deadline:
            sleep(_AUTH_PROPAGATION_WAIT_SECONDS)
            continue
        raise BackupProvisionError(
            f"restic init failed (exit {attempt.returncode}): {attempt.stderr.strip()}"
        )


def provision_from_file(env_file: Path) -> bool:
    if not env_file.is_file():
        raise BackupProvisionError(f"restic env file not found: {env_file}")
    return initialize_repository(parse_restic_env_file(env_file.read_text()))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Initialize the workspace restic repository."
    )
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
    sys.stdout.write(
        "repository initialized\n"
        if was_created
        else "repository already initialized\n"
    )


if __name__ == "__main__":
    main()
