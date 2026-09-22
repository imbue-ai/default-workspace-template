import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

import pytest
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from pydantic import Field

_PROBE_SCRIPT: Final[str] = "#!/bin/sh\nexit 0\n"
_PROBE_TIMEOUT_SECONDS: Final[float] = 10.0
# Off the backed-up home volume, and short: pytest nests tmp_path several levels
# under this root, and a unix socket a test binds there must fit AF_UNIX's path
# limit (104 bytes on macOS, 108 on Linux).
_FALLBACK_ROOT: Final[Path] = Path("/var/tmp")


class TempRootSelection(FrozenModel):
    """Which temp root a session can run its files from, and the candidates that could not."""

    usable_root: Path | None = Field(
        description="The first candidate a written file can run from, if any"
    )
    rejected_roots: tuple[Path, ...] = Field(
        description="Candidates tried before it that cannot run files"
    )


_SELECTION_KEY: Final[pytest.StashKey[TempRootSelection]] = pytest.StashKey[
    TempRootSelection
]()


def can_run_files_in(directory: Path) -> bool:
    """Whether a script written under ``directory`` runs: false on a noexec mount, or where nothing can be written."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe_dir = Path(tempfile.mkdtemp(prefix="executable-probe-", dir=directory))
    except OSError:
        return False
    try:
        probe = probe_dir / "probe"
        probe.write_text(_PROBE_SCRIPT)
        probe.chmod(0o700)
        try:
            return (
                subprocess.run(
                    [str(probe)], check=False, timeout=_PROBE_TIMEOUT_SECONDS
                ).returncode
                == 0
            )
        except PermissionError:
            return False
    finally:
        shutil.rmtree(probe_dir)


def select_temp_root(
    default_root: Path,
    explicit_root: Path | None,
    fallback_root: Path,
    can_run: Callable[[Path], bool],
) -> TempRootSelection:
    """Pick the first candidate root that can run files. A root chosen explicitly is never replaced."""
    candidates = (
        (explicit_root,) if explicit_root is not None else (default_root, fallback_root)
    )
    rejected: list[Path] = []
    for candidate in candidates:
        if can_run(candidate):
            return TempRootSelection(
                usable_root=candidate, rejected_roots=tuple(rejected)
            )
        rejected.append(candidate)
    return TempRootSelection(usable_root=None, rejected_roots=tuple(rejected))


def _explicit_temp_root(config: pytest.Config) -> Path | None:
    # pytest creates (and empties) --basetemp itself, so what must allow running
    # files is the directory it is created in.
    if config.option.basetemp is not None:
        return Path(config.option.basetemp).resolve().parent
    from_env = os.environ.get("PYTEST_DEBUG_TEMPROOT")
    return Path(from_env) if from_env else None


@pure
def _no_usable_root_message(rejected_roots: Sequence[Path]) -> str:
    listed = ", ".join(str(root) for root in rejected_roots)
    return (
        f"pytest-executable-tmp: a file written under {listed} cannot be run (a noexec mount?). A test's stub "
        "executable on PATH would be skipped there, and the real program it stands in for would run instead. "
        "Point --basetemp, PYTEST_DEBUG_TEMPROOT or TMPDIR (whichever this run uses) at a directory that allows "
        "running files."
    )


def pytest_configure(config: pytest.Config) -> None:
    selection = select_temp_root(
        default_root=Path(tempfile.gettempdir()),
        explicit_root=_explicit_temp_root(config),
        fallback_root=_FALLBACK_ROOT,
        can_run=can_run_files_in,
    )
    if selection.usable_root is None:
        pytest.exit(
            _no_usable_root_message(selection.rejected_roots),
            returncode=pytest.ExitCode.USAGE_ERROR,
        )
    if selection.rejected_roots:
        # tmp_path's root is read from tempfile lazily, at first use; TMPDIR
        # carries the same root to every subprocess a test starts.
        tempfile.tempdir = str(selection.usable_root)
        os.environ["TMPDIR"] = str(selection.usable_root)
        config.stash[_SELECTION_KEY] = selection


def pytest_report_header(config: pytest.Config) -> str | None:
    selection = config.stash.get(_SELECTION_KEY, None)
    if selection is None:
        return None
    rejected = ", ".join(str(root) for root in selection.rejected_roots)
    return f"temporary files: {selection.usable_root} (files under {rejected} cannot be run)"
