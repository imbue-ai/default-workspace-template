"""The tk SessionStart hook links the vendored script in only when nothing on PATH answers.

A worker runs in a worktree under the workspace's HOME. Linking unconditionally from
there pointed every shell's ``tk`` at the worker's checkout, which is removed with the
worker.
"""

import os
import subprocess
from pathlib import Path

_HOOK = Path(__file__).resolve().with_name("ensure_tk_on_path.sh")


def _checkout_with_tk(root: Path) -> Path:
    script = root / "system" / "vendor" / "tk" / "ticket"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\necho ticket\n")
    script.chmod(0o755)
    return script


def _run_hook(work_dir: Path, home: Path, path: str) -> None:
    subprocess.run(
        ["bash", str(_HOOK)],
        env={
            **os.environ,
            "MNGR_AGENT_WORK_DIR": str(work_dir),
            "HOME": str(home),
            "PATH": path,
        },
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_a_tk_already_on_path_is_left_alone(tmp_path: Path) -> None:
    # The image's /usr/local/bin/tk stands in for the baked link; the worktree's
    # copy must not shadow it from ~/.local/bin.
    home = tmp_path / "home"
    home.mkdir()
    baked_bin = tmp_path / "usr-local-bin"
    baked_bin.mkdir()
    baked = baked_bin / "tk"
    baked.write_text("#!/bin/sh\necho baked\n")
    baked.chmod(0o755)
    (baked_bin / "ticket").symlink_to(baked)
    _checkout_with_tk(tmp_path / "worktree")

    _run_hook(tmp_path / "worktree", home, f"{baked_bin}:/usr/bin:/bin")

    assert not (home / ".local" / "bin" / "tk").exists()
    assert not (home / ".local" / "bin" / "ticket").exists()


def test_a_link_left_dangling_by_a_removed_worktree_is_repointed(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    local_bin = home / ".local" / "bin"
    local_bin.mkdir(parents=True)
    gone = (
        tmp_path
        / "worktrees"
        / "update-self-old"
        / "system"
        / "vendor"
        / "tk"
        / "ticket"
    )
    for name in ("tk", "ticket"):
        (local_bin / name).symlink_to(gone)
    script = _checkout_with_tk(tmp_path / "workspace")

    _run_hook(tmp_path / "workspace", home, f"{local_bin}:/usr/bin:/bin")

    assert os.readlink(local_bin / "tk") == str(script)
    assert os.readlink(local_bin / "ticket") == str(script)
