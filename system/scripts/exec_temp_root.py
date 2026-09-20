"""Find a temp root that can execute what is written into it, and point Python at it.

A workspace container mounts `/tmp` `noexec` (Docker's `--tmpfs` default, applied by
the docker provider block in `.mngr/settings.toml` and by the imbue_cloud slice bake).
Several of this repo's suites test a script the way it is really used: write a stub
binary into a temp dir, put that dir on `PATH`, and run the real thing against it. Under
a `noexec` temp root those stubs cannot run -- and because `execvp` *skips* a `PATH`
entry it cannot execute rather than reporting it, the search falls through to the real
binary, so the failure surfaces as an unrelated-looking assertion rather than as a
permission error.

This lives in `system/scripts/` and is stdlib-only, so each of the repo's three pytest
roots can load it by path: the root `conftest.py`, and the `chat` and
`system_interface` apps, which the root pytest config ignores because they carry their
own.
"""

import os
import subprocess
import tempfile
from pathlib import Path


class NoExecutableTempRootError(Exception):
    """No candidate temp root could execute a file written into it."""


def fallback_temp_roots() -> tuple[Path, ...]:
    """Candidate temp roots when the default cannot execute, in the order tried.

    `/var/tmp` is the alternate the FHS already guarantees; the cache dir is the
    workspace's own writable space, for an image that hardens `/var/tmp` the same way.
    Read at call time rather than at import, since `Path.home()` depends on the
    environment the run was launched with.
    """
    return (Path("/var/tmp"), Path.home() / ".cache" / "pytest-temp")


def can_execute_a_file_in(directory: Path) -> bool:
    """Whether a file written into `directory` can then be run.

    Answered by running one, not by reading the mount's flags: `noexec` is the usual
    reason a directory refuses, but a `noexec`-free mount can still refuse (a filesystem
    with no execute bit at all, an LSM), and what a test planting a stub binary on PATH
    needs to know is the outcome.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(dir=directory, suffix=".sh")
    except OSError:
        return False
    probe = Path(name)
    try:
        with os.fdopen(handle, "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        probe.chmod(0o700)
        return subprocess.run([str(probe)], capture_output=True).returncode == 0
    except OSError:
        return False
    finally:
        probe.unlink(missing_ok=True)


def relocate_temp_root_if_needed() -> Path | None:
    """Move the process's temp root to one that can execute; return it, or None if unmoved.

    Raises :class:`NoExecutableTempRootError` when no candidate works, rather than
    letting the suites run into the same wall with a less legible failure.
    """
    default_root = Path(tempfile.gettempdir())
    if can_execute_a_file_in(default_root):
        return None
    for candidate in fallback_temp_roots():
        if can_execute_a_file_in(candidate):
            # `tempfile.tempdir` is what pytest's `tmp_path` root resolves through, and
            # it caches, so setting only the environment variable would be too late for
            # an already-resolved default. TMPDIR is for the subprocesses tests spawn.
            tempfile.tempdir = str(candidate)
            os.environ["TMPDIR"] = str(candidate)
            return candidate
    tried = ", ".join(str(root) for root in (default_root, *fallback_temp_roots()))
    raise NoExecutableTempRootError(
        f"no temp directory here can execute a file it holds (tried {tried}). Suites in "
        "this repo run stub binaries they write to one, so they cannot pass as-is; pass "
        "--basetemp naming a directory on a filesystem mounted without noexec."
    )
