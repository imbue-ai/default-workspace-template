#!/usr/bin/env python3
"""Rebuild the system-interface frontend bundle if its sources changed.

Reveal-time helper for the ``update-system-interface`` flow, run from the
``system_interface`` service command in ``services.toml`` (alongside
``forward_port.py``). The built bundle in
``apps/system_interface/imbue/system_interface/static/`` is gitignored, so
merging a worker's frontend change brings new ``frontend/src`` but a stale (or
absent) built bundle. On service startup this script hashes the frontend build
inputs, compares them against the hash recorded beside the last build, and runs
``npm run build`` only when they differ (or when the bundle is missing). A
backend-only reveal therefore skips the build entirely.

It is deliberately **best-effort**: if the build fails (or ``npm`` is missing) it
logs loudly and still exits 0, so the service starts and serves whatever bundle
already exists rather than leaving the user with no UI at all. The worker has
already verified a clean build before merge, so a failure here is an unexpected,
environment-specific event -- failing open is safer for a live UI than failing
closed.

Run via bare ``python3`` (like ``forward_port.py``), so this uses only the
standard library.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

# scripts/ sits at the repo root, so the repo root is one level up.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_FRONTEND_DIR = _REPO_ROOT / "apps" / "system_interface" / "frontend"
_STATIC_DIR = _REPO_ROOT / "apps" / "system_interface" / "imbue" / "system_interface" / "static"
_HASH_FILE = _STATIC_DIR / ".build_inputs_hash"

# Build-affecting inputs: everything under these directories plus these
# top-level files (the ones Vite reads to produce the bundle). ``media`` is the
# Vite ``publicDir`` copied verbatim into the output, so it counts; lint-only
# config (e.g. ``eslint.config.js``) does not, since it never changes the bundle.
_INPUT_DIRS = ("src", "media")
_INPUT_FILES = ("index.html", "vite.config.ts", "tsconfig.json", "package.json", "package-lock.json")

_LOG_PREFIX = "[build_frontend_if_changed]"


def _log(message: str) -> None:
    sys.stderr.write(f"{_LOG_PREFIX} {message}\n")


def compute_inputs_hash(frontend_dir: Path) -> str:
    """A content hash over every build-affecting input under ``frontend_dir``.

    Path-and-content based and order-independent (paths are sorted), so the hash
    changes iff some input's relative path or bytes change.
    """
    digest = hashlib.sha256()
    paths: list[Path] = []
    for directory in _INPUT_DIRS:
        root = frontend_dir / directory
        if root.is_dir():
            paths.extend(path for path in root.rglob("*") if path.is_file())
    for filename in _INPUT_FILES:
        path = frontend_dir / filename
        if path.is_file():
            paths.append(path)
    for path in sorted(paths):
        relative = path.relative_to(frontend_dir).as_posix()
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def needs_build(frontend_dir: Path, static_dir: Path, hash_file: Path) -> tuple[bool, str]:
    """Decide whether a rebuild is needed, returning ``(needed, current_hash)``.

    A build is needed when the bundle is missing, the recorded hash is missing,
    or the recorded hash differs from the current inputs hash.
    """
    current = compute_inputs_hash(frontend_dir)
    if not (static_dir / "index.html").is_file():
        return True, current
    if not hash_file.is_file():
        return True, current
    return hash_file.read_text().strip() != current, current


def _run_npm_build(frontend_dir: Path) -> None:
    if not (frontend_dir / "node_modules").is_dir():
        subprocess.run(["npm", "ci"], cwd=frontend_dir, check=True)
    subprocess.run(["npm", "run", "build"], cwd=frontend_dir, check=True)


def main() -> int:
    if not _FRONTEND_DIR.is_dir():
        _log(f"no frontend dir at {_FRONTEND_DIR}; nothing to build")
        return 0

    build_needed, current_hash = needs_build(_FRONTEND_DIR, _STATIC_DIR, _HASH_FILE)
    if not build_needed:
        _log("frontend bundle is up to date; skipping build")
        return 0

    _log("frontend sources changed; rebuilding bundle...")
    try:
        _run_npm_build(_FRONTEND_DIR)
    except (subprocess.CalledProcessError, FileNotFoundError) as error:
        _log(f"WARNING: frontend build failed ({error}); serving the existing bundle")
        return 0

    _STATIC_DIR.mkdir(parents=True, exist_ok=True)
    _HASH_FILE.write_text(current_hash)
    _log("frontend rebuilt; bundle updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
