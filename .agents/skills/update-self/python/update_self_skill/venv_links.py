"""Keep every project's ``.venv`` a link to a directory next to the uv cache.

uv installs a venv by hardlinking files out of its cache, but only when the venv is on
the cache's filesystem; anywhere else it falls back to a full copy. The workspace lives
on a volume and the cache on the container's root filesystem, so a venv in the checkout
is a copy of everything it holds (the root one alone is about 512M). Each project's
``.venv`` is therefore a symlink to a target under ``<uv cache dir>/../venvs``, which
sits beside the cache, and uv hardlinks into it.

A target lives on the root filesystem, so a rebuilt one (a recreated workspace, a
restore onto a new container) loses it while the checkout keeps the link. uv refuses a
link whose target is missing ("File exists"), so every program that syncs runs
``ensure`` first, which recreates it.

Each target records the link it serves in a sibling ``<target>.owner.json`` (beside it,
not inside it: uv refuses a non-empty directory that is not a venv). ``sweep`` deletes
every target whose link is gone or points elsewhere, so a worktree removed by anything
at all leaks nothing past the next run. A target being set up is safe from a concurrent
sweep because its link and owner record are written before the directory, and the
update apply's fresh target, which no link names yet, records the apply's pid as its
holder: a sweep keeps a target whose holder is alive.

Two programs need this: the boot, worktree, build and session-start paths (through the
CLI below), and the update apply, which is staged and run as a self-contained unit and
so cannot import from ``system/scripts``. This file therefore exists twice, byte for
byte:

    system/scripts/workspace_bare_scripts/venv_links.py
    .agents/skills/update-self/python/update_self_skill/venv_links.py

``system/scripts/update_self_mirrors_sync_test.py`` fails if they differ; edit one and
copy it over the other.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import NamedTuple, Protocol

VENV_NAME = ".venv"
VENVS_DIR_NAME = "venvs"
OWNER_SUFFIX = ".owner.json"

# Where a real ``.venv`` directory waits while its replacement is synced. ``.venv-*`` is
# gitignored, so it never dirties the tree.
CONVERTING_NAME = ".venv-converting"
SWITCHING_NAME = ".venv-switching"

# The sync that fills a converted project's target: the root workspace syncs every member
# from the committed lock, a standalone project its own lock.
ROOT_SYNC = ("uv", "sync", "--all-packages", "--frozen")
PROJECT_SYNC = ("uv", "sync", "--locked")

_KEY_MAX_LENGTH = 60
_KEY_HASH_LENGTH = 10


class Completed(Protocol):
    @property
    def returncode(self) -> int: ...

    @property
    def stdout(self) -> str: ...

    @property
    def stderr(self) -> str: ...


class Run(Protocol):
    """How the helper reaches uv, injected so a test can stand in for it."""

    def __call__(self, command: Sequence[str], /, *, cwd: Path) -> Completed: ...


def run_subprocess(command: Sequence[str], /, *, cwd: Path) -> Completed:
    return subprocess.run(
        list(command), cwd=cwd, capture_output=True, text=True, check=False
    )


def is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Owner(NamedTuple):
    """What a target's owner record says: the link it serves, and the process holding it."""

    link: str
    holder_pid: int | None


def uv_cache_dir(run: Run, cwd: Path) -> Path:
    result = run(["uv", "cache", "dir"], cwd=cwd)
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(
            f"`uv cache dir` failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return Path(result.stdout.strip())


def venvs_root(cache_dir: Path) -> Path:
    return Path(os.path.realpath(cache_dir.parent)) / VENVS_DIR_NAME


def target_for(root: Path, project_dir: Path) -> Path:
    """The target a project's link names: a readable key from its path, plus a short hash."""
    absolute = os.path.abspath(project_dir)
    readable = re.sub(r"[^A-Za-z0-9]+", "-", absolute).strip("-")
    if len(readable) > _KEY_MAX_LENGTH:
        tail = readable[-_KEY_MAX_LENGTH:]
        readable = tail.split("-", 1)[1] if "-" in tail else tail
    digest = hashlib.sha256(absolute.encode()).hexdigest()[:_KEY_HASH_LENGTH]
    return root / f"{readable}-{digest}"


def owner_path(target: Path) -> Path:
    return target.with_name(target.name + OWNER_SUFFIX)


def write_owner(target: Path, link: Path, holder_pid: int | None = None) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    path = owner_path(target)
    scratch = path.with_name(path.name + f".{os.getpid()}.tmp")
    scratch.write_text(
        json.dumps({"link": os.path.abspath(link), "holder_pid": holder_pid}) + "\n"
    )
    os.replace(scratch, path)


def read_owner(target: Path) -> Owner | None:
    try:
        data = json.loads(owner_path(target).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("link"), str):
        return None
    holder = data.get("holder_pid")
    return Owner(
        link=data["link"], holder_pid=holder if isinstance(holder, int) else None
    )


def hold(target: Path, holder_pid: int | None) -> None:
    """Record ``holder_pid`` as the process keeping ``target`` (``None`` releases it)."""
    owner = read_owner(target)
    if owner is not None:
        write_owner(target, Path(owner.link), holder_pid)


def _point_link(link: Path, target: Path) -> None:
    """Make ``link`` a symlink to ``target`` in one rename, replacing any link already there."""
    scratch = link.with_name(SWITCHING_NAME)
    if scratch.is_symlink() or scratch.exists():
        scratch.unlink()
    scratch.symlink_to(target)
    os.replace(scratch, link)


def _new_target(link: Path, target: Path, holder_pid: int | None = None) -> None:
    """Point ``link`` at ``target`` and create it, in the order a concurrent sweep needs:
    the link and the owner record exist before the directory does."""
    _point_link(link, target)
    write_owner(target, link, holder_pid)
    target.mkdir(parents=True, exist_ok=True)


def remove_target(target: Path) -> None:
    shutil.rmtree(target, ignore_errors=True)
    owner_path(target).unlink(missing_ok=True)


def _serves(owner: Owner, target: Path) -> bool:
    link = Path(owner.link)
    return link.is_symlink() and os.path.realpath(link) == os.path.realpath(target)


def _is_held(owner: Owner, alive: Callable[[int], bool]) -> bool:
    return owner.holder_pid is not None and alive(owner.holder_pid)


def sweep(root: Path, alive: Callable[[int], bool] = is_pid_alive) -> list[Path]:
    """Delete every target whose link is gone or points elsewhere, and every owner
    record left without a target it could still be waiting for. Returns what was removed."""
    if not root.is_dir():
        return []
    removed: list[Path] = []
    for entry in sorted(root.iterdir()):
        # Removing a target removes its owner record too, which this listing still holds.
        if not entry.exists() and not entry.is_symlink():
            continue
        if entry.name.endswith(OWNER_SUFFIX):
            target = entry.with_name(entry.name[: -len(OWNER_SUFFIX)])
            if target.exists():
                continue
            owner = read_owner(target)
            # An owner record without its directory is a target being set up (its link
            # already names it) or the remains of one: keep the first.
            if owner is None or not (_serves(owner, target) or _is_held(owner, alive)):
                entry.unlink(missing_ok=True)
                removed.append(entry)
            continue
        if entry.is_symlink() or not entry.is_dir():
            continue
        owner = read_owner(entry)
        if owner is not None and (_serves(owner, entry) or _is_held(owner, alive)):
            continue
        remove_target(entry)
        removed.append(entry)
    return removed


def fresh_target(root: Path, project_dir: Path, holder_pid: int) -> Path:
    """A new, empty target for ``project_dir`` that no link names yet, held by ``holder_pid``."""
    base = target_for(root, project_dir)
    target = base.with_name(f"{base.name}-{secrets.token_hex(4)}")
    write_owner(target, project_dir / VENV_NAME, holder_pid)
    target.mkdir(parents=True)
    return target


def switch_link(project_dir: Path, target: Path) -> None:
    """Point ``project_dir``'s ``.venv`` at ``target``. The link must not be a real directory."""
    link = project_dir / VENV_NAME
    if link.exists() and not link.is_symlink():
        raise IsADirectoryError(f"{link} is a real directory, not a link")
    _point_link(link, target)


def check_same_filesystem(root: Path, cache_dir: Path) -> bool:
    """Warn on stderr when the targets cannot hardlink from the cache. Returns whether they can."""
    root.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    if os.stat(root).st_dev == os.stat(cache_dir).st_dev:
        return True
    sys.stderr.write(
        f"warning: the venvs at {root} are on a different filesystem from the uv cache "
        f"at {cache_dir}, so uv copies every file into them instead of hardlinking it.\n"
    )
    return False


def _convert(project_dir: Path, root: Path, sync: Sequence[str], run: Run) -> bool:
    """Replace a real ``.venv`` directory with a linked one synced by ``sync``.

    The old directory waits beside the link until the sync succeeds and is deleted then;
    if the sync fails it goes back, so a project offline keeps a working venv until a
    later run.
    """
    link = project_dir / VENV_NAME
    aside = project_dir / CONVERTING_NAME
    if aside.exists():
        shutil.rmtree(aside)
    os.rename(link, aside)
    target = target_for(root, project_dir)
    remove_target(target)
    _new_target(link, target)
    result = run(list(sync), cwd=project_dir)
    if result.returncode == 0:
        shutil.rmtree(aside, ignore_errors=True)
        return True
    link.unlink()
    remove_target(target)
    os.rename(aside, link)
    sys.stderr.write(
        f"warning: could not convert {link} to a linked venv: `{' '.join(sync)}` failed "
        f"(exit {result.returncode}); kept the real directory.\n"
        f"{(result.stderr or result.stdout).strip()}\n"
    )
    return False


def ensure_link(project_dir: Path, root: Path, sync: Sequence[str], run: Run) -> None:
    """Make ``project_dir``'s ``.venv`` a link to an existing target, creating the target
    empty when it is missing; only a real directory being converted is synced here."""
    link = project_dir / VENV_NAME
    aside = project_dir / CONVERTING_NAME
    # A conversion that was interrupted: put the real directory back and convert again.
    if aside.is_dir() and not aside.is_symlink():
        if link.is_symlink():
            link.unlink()
        if not link.exists():
            os.rename(aside, link)
    if link.is_symlink():
        resolved = Path(os.path.realpath(link))
        if resolved.is_dir():
            if resolved.parent == root and read_owner(resolved) is None:
                write_owner(resolved, link)
            return
        _new_target(link, target_for(root, project_dir))
        return
    if link.is_dir():
        _convert(project_dir, root, sync, run)
        return
    if link.exists():
        sys.stderr.write(
            f"warning: {link} is neither a directory nor a link; left it.\n"
        )
        return
    _new_target(link, target_for(root, project_dir))


def ensure(
    checkout_root: Path | None,
    project_dirs: Sequence[Path],
    run: Run = run_subprocess,
    alive: Callable[[int], bool] = is_pid_alive,
) -> None:
    """Link ``checkout_root``'s venv (unless it is ``None``) and each standalone
    project's, then sweep."""
    cache_dir = uv_cache_dir(run, checkout_root or project_dirs[0])
    root = venvs_root(cache_dir)
    root.mkdir(parents=True, exist_ok=True)
    if checkout_root is not None:
        ensure_link(checkout_root, root, ROOT_SYNC, run)
    for project_dir in project_dirs:
        ensure_link(project_dir, root, PROJECT_SYNC, run)
    sweep(root, alive)
    check_same_filesystem(root, cache_dir)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Keep every project's .venv a link to a directory next to the uv cache."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    ensure_parser = commands.add_parser(
        "ensure", help="link a checkout's venv (and standalone projects'), then sweep"
    )
    ensure_parser.add_argument("checkout_root", type=Path)
    ensure_parser.add_argument("project_dirs", type=Path, nargs="*")
    project_parser = commands.add_parser(
        "ensure-project",
        help="link standalone projects' venvs only (a user program's start), then sweep",
    )
    project_parser.add_argument("project_dirs", type=Path, nargs="+")
    commands.add_parser("sweep", help="delete targets no link serves")
    fresh_parser = commands.add_parser(
        "fresh", help="print a new empty target for a project, held by a pid"
    )
    fresh_parser.add_argument("project_dir", type=Path)
    fresh_parser.add_argument("--holder-pid", type=int, default=os.getppid())
    switch_parser = commands.add_parser(
        "switch", help="point a project's .venv at a target"
    )
    switch_parser.add_argument("project_dir", type=Path)
    switch_parser.add_argument("target", type=Path)
    args = parser.parse_args(argv)

    cwd = Path.cwd()
    try:
        if args.command == "ensure":
            ensure(
                args.checkout_root.resolve(),
                [path.resolve() for path in args.project_dirs],
            )
        elif args.command == "ensure-project":
            ensure(None, [path.resolve() for path in args.project_dirs])
        elif args.command == "sweep":
            for removed in sweep(venvs_root(uv_cache_dir(run_subprocess, cwd))):
                print(f"removed {removed}")
        elif args.command == "fresh":
            root = venvs_root(uv_cache_dir(run_subprocess, cwd))
            print(fresh_target(root, args.project_dir.resolve(), args.holder_pid))
        elif args.command == "switch":
            switch_link(args.project_dir.resolve(), args.target)
    except (OSError, RuntimeError) as exc:
        sys.stderr.write(f"venv_links {args.command}: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
