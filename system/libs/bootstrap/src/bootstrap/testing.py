"""Shared helpers for the bootstrap unit tests.

The installer scripts under test (system/scripts/install_secret_scanners.sh and
system/scripts/deferred_install.sh) are bash, so each test sources one in a fresh
bash process (the scripts only run `main` when executed directly) and
exercises individual functions with test-controlled overrides:

- env vars (DEFERRED_INSTALL_MARKER_DIR / SECRET_SCANNER_INSTALL_DIR) point
  into tmp_path;
- a stub-bin dir is prepended to PATH with a fake `curl` (serves a prepared
  tarball and logs its invocation) and a fake `uname` (fixed architecture),
  so tests need no network access and no dependence on the host's arch.
"""

from __future__ import annotations

import json
import stat
import subprocess
import tarfile
from collections.abc import Callable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]

FAKE_BINARY_CONTENT = b"#!/bin/sh\necho fake-scanner\n"


def make_scanner_tarball(path: Path, member: str) -> None:
    """Write a gzipped tarball containing a single binary member named `member`."""
    binary = path.parent / member
    binary.write_bytes(FAKE_BINARY_CONTENT)
    with tarfile.open(path, "w:gz") as tar:
        tar.add(binary, arcname=member)
    binary.unlink()


def make_stub_bin(stub_bin: Path, *, served_tarball: Path | None, arch: str) -> Path:
    """Create stub `curl` and `uname` executables; return the curl call log path."""
    stub_bin.mkdir(parents=True, exist_ok=True)
    curl_log = stub_bin / "curl_calls.log"
    if served_tarball is None:
        # No tarball prepared: any curl call fails (and is still logged).
        curl_body = f'#!/bin/bash\necho "$@" >> "{curl_log}"\nexit 22\n'
    else:
        curl_body = (
            "#!/bin/bash\n"
            f'echo "$@" >> "{curl_log}"\n'
            'out=""\n'
            'while [ "$#" -gt 0 ]; do\n'
            '    if [ "$1" = "-o" ]; then out="$2"; shift 2; else shift; fi\n'
            "done\n"
            f'cp "{served_tarball}" "$out"\n'
        )
    (stub_bin / "curl").write_text(curl_body)
    (stub_bin / "uname").write_text(f'#!/bin/bash\necho "{arch}"\n')
    for name in ("curl", "uname"):
        exe = stub_bin / name
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return curl_log


def run_sourced(
    script: Path,
    snippet: str,
    *,
    extra_env: dict[str, str],
    stub_bin: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Source `script` in a fresh bash and run `snippet`."""
    path_prefix = f"{stub_bin}:" if stub_bin is not None else ""
    full_script = f'source "{script}"\n{snippet}\n'
    return subprocess.run(
        ["bash", "-c", full_script],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": f"{path_prefix}/usr/bin:/bin:/usr/sbin:/sbin",
            **extra_env,
        },
    )


def install_fake_pinned_scanner(install_dir: Path, script: Path, tool: str) -> None:
    """Drop a fake `tool` binary into `install_dir` that reports the pinned version.

    The pinned version is read from the script under test (via
    `_scanner_pinned_version`), so tests never duplicate the pins.
    """
    install_dir.mkdir(parents=True, exist_ok=True)
    result = run_sourced(script, f"_scanner_pinned_version {tool}", extra_env={})
    assert result.returncode == 0, result.stderr
    pinned = result.stdout.strip()
    assert pinned
    fake = install_dir / tool
    fake.write_text(f'#!/bin/sh\necho "{tool} {pinned}"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class StubSubprocess:
    """Capture-and-replay double for ``subprocess.run``.

    ``calls`` and ``kwargs`` record each invocation. ``on_command`` (when set)
    runs for every argv, which is how the recovery tests model the script
    clearing the apply marker on a successful rollback. ``raise_on`` maps a
    token in the argv to the exception that call raises, for the "the
    executable is missing / hangs" paths. ``reply_on`` maps a token in the argv
    to the exit code and stdout that call returns instead of the defaults.
    """

    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""
        self.calls: list[list[str]] = []
        self.kwargs: list[dict[str, object]] = []
        self.on_command: Callable[[list[str]], None] | None = None
        self.raise_on: dict[str, BaseException] = {}
        self.reply_on: dict[str, tuple[int, str]] = {}

    def run(self, cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        self.kwargs.append(dict(kwargs))
        if self.on_command is not None:
            self.on_command(cmd)
        for token, error in self.raise_on.items():
            if token in cmd:
                raise error
        returncode, stdout = next(
            (reply for token, reply in self.reply_on.items() if token in cmd),
            (self.returncode, self.stdout),
        )
        return subprocess.CompletedProcess(
            args=cmd, returncode=returncode, stdout=stdout, stderr=self.stderr
        )


MNGR_GIT_URL = "https://github.com/imbue-ai/mngr"


def install_package(
    venv: Path, name: str, entry_points: str, direct_url: dict[str, object] | None
) -> None:
    """Write the installed metadata of package ``name`` into ``venv``'s site-packages, as uv leaves it."""
    dist_info = (
        venv
        / "lib"
        / "python3.12"
        / "site-packages"
        / f"{name.replace('-', '_')}-0.1.0.dist-info"
    )
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: 0.1.0\n"
    )
    (dist_info / "entry_points.txt").write_text(entry_points)
    if direct_url is not None:
        (dist_info / "direct_url.json").write_text(json.dumps(direct_url))


def install_mngr(venv: Path, commit: str) -> dict[str, str]:
    """Install the metadata of mngr, one plugin and an unrelated package into ``venv`` from ``commit``; the
    identity ``installed_mngr`` reads off them."""

    def from_commit(subdirectory: str) -> dict[str, object]:
        return {
            "url": MNGR_GIT_URL,
            "vcs_info": {"vcs": "git", "commit_id": commit},
            "subdirectory": subdirectory,
        }

    install_package(
        venv,
        "imbue-mngr",
        "[console_scripts]\nmngr = imbue.mngr.main:cli\n",
        from_commit("libs/mngr"),
    )
    install_package(
        venv,
        "imbue-mngr-claude",
        "[mngr]\nclaude = imbue.mngr_claude.plugin\n",
        from_commit("libs/mngr_claude"),
    )
    install_package(venv, "loguru", "", None)
    return {
        "imbue-mngr": f"{MNGR_GIT_URL}@{commit}",
        "imbue-mngr-claude": f"{MNGR_GIT_URL}@{commit}",
    }
