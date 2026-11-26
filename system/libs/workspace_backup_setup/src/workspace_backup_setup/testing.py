"""A stand-in ``restic`` and a clock a test moves, for the tests of everything that sets up backups."""

import os
import stat
from pathlib import Path
from typing import Final

# Read by the stand-in: the directory holding its ``mode`` file, its ``calls`` log, the ``refused-tokens`` it
# turns away, and the ``release`` file a held run waits for.
FAKE_RESTIC_CONTROL_DIR_ENV: Final[str] = "FAKE_RESTIC_CONTROL_DIR"

# What the stand-in does, by the word in its ``mode`` file.
FAKE_RESTIC_CREATES: Final[str] = "ok"
FAKE_RESTIC_FINDS_A_REPOSITORY: Final[str] = "exists"
FAKE_RESTIC_FINDS_ANOTHER_PASSWORDS_REPOSITORY: Final[str] = "wrong-password"
FAKE_RESTIC_FAILS: Final[str] = "fail"
FAKE_RESTIC_HOLDS: Final[str] = "hold"

_FAKE_RESTIC_SCRIPT: Final[str] = """#!/bin/sh
control="$FAKE_RESTIC_CONTROL_DIR"
echo "$RESTIC_REPOSITORY|$*" >> "$control/calls"
mode="$(cat "$control/mode" 2>/dev/null || echo ok)"
if [ "$mode" = "hold" ]; then
  while [ ! -f "$control/release" ]; do sleep 0.02; done
  exit 0
fi
if [ -n "$RESTIC_REST_PASSWORD" ] && grep -qxF "$RESTIC_REST_PASSWORD" "$control/refused-tokens" 2>/dev/null; then
  echo "Fatal: unable to open config file: unexpected HTTP response (401): 401 Unauthorized" >&2
  exit 1
fi
if [ "$mode" = "fail" ]; then
  echo "Fatal: could not reach $RESTIC_REPOSITORY with password $RESTIC_PASSWORD" >&2
  exit 1
fi
if [ "$1" = "init" ]; then
  case "$mode" in
    ok) echo "created restic repository"; exit 0;;
    *) echo "Fatal: create repository at $RESTIC_REPOSITORY failed: config file already exists" >&2; exit 1;;
  esac
fi
case "$mode" in
  ok|exists) exit 0;;
  wrong-password) echo "Fatal: wrong password or no key found" >&2; exit 1;;
esac
echo "Fatal: unexpected arguments: $*" >&2
exit 1
"""


def install_fake_restic(bin_directory: Path, control_directory: Path) -> None:
    """Write the stand-in as ``restic`` in ``bin_directory``, and make its control directory."""
    bin_directory.mkdir(parents=True, exist_ok=True)
    control_directory.mkdir(parents=True, exist_ok=True)
    script = bin_directory / "restic"
    script.write_text(_FAKE_RESTIC_SCRIPT)
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def set_fake_restic_mode(control_directory: Path, mode: str) -> None:
    (control_directory / "mode").write_text(mode)


def release_fake_restic(control_directory: Path) -> None:
    """Let a held run finish."""
    (control_directory / "release").write_text("")


def fake_restic_calls(control_directory: Path) -> list[str]:
    """Each run of the stand-in so far, as ``<repository>|<arguments>``."""
    try:
        return (control_directory / "calls").read_text().splitlines()
    except FileNotFoundError:
        return []


def refuse_fake_restic_token(control_directory: Path, token: str) -> None:
    """Have the stand-in's storage turn away every run that shows ``token`` as ``RESTIC_REST_PASSWORD``."""
    with (control_directory / "refused-tokens").open("a") as out:
        out.write(token + "\n")


def restic_env_text(repository: str, password: str) -> str:
    """A ``restic.env`` naming ``repository`` and its password, with one backend key."""
    return f"RESTIC_REPOSITORY={repository}\nRESTIC_REST_USERNAME=workspace\nRESTIC_PASSWORD={password}\n"


def path_with_fake_restic_first(bin_directory: Path) -> str:
    return f"{bin_directory}{os.pathsep}{os.environ.get('PATH', '')}"


class SettableClock:
    """A clock a test moves."""

    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


WORKSPACE_ID: Final[str] = "agent-7d1f0c3a9b2e4f6581a0c4d2e6f8b1a3"
IMBUE_CLOUD_REPOSITORY: Final[str] = (
    f"rest:https://backups.example/user-41c2/{WORKSPACE_ID}/"
)


def imbue_cloud_request(token: str) -> dict[str, object]:
    """A request for Imbue Cloud with the storage the connector would issue this workspace, under ``token``."""
    return {
        "destination": "IMBUE_CLOUD",
        "imbue_cloud_storage": {
            "repository_url": IMBUE_CLOUD_REPOSITORY,
            "rest_username": WORKSPACE_ID,
            "rest_password": token,
        },
    }


def own_storage_request(
    repository: str, backend_env: dict[str, str]
) -> dict[str, object]:
    """A request for storage of the user's own."""
    return {
        "destination": "OWN_STORAGE",
        "own_storage": {"repository": repository, "backend_env": backend_env},
    }
