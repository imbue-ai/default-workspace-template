#!/usr/bin/env python3
"""Validate the inspiration assembled at a repo root. The publish flow's gate.

Checks, in one command:

  - `inspiration.toml` parses and satisfies the schema;
  - `inspiration.md` and `inspiration.toml` agree (front matter, prerequisites);
  - the thumbnail exists and is not still the generated placeholder;
  - no `<!-- FILL-IN (publishing agent)` block was left unreplaced anywhere;
  - every declared env.d unit is well-named and actually ships in the snapshot;
  - every declared apt package resolves in the mirrored universe at the
    publisher's pinned snapshot timestamp.

That last check is the reason unmirrorable third-party packages are rejected
here rather than at some adopter's first boot.

Run from the repo root being validated:

    uv run --no-project --with 'pydantic>=2' python validate_inspiration.py [REPO_ROOT]

`--no-project` is load-bearing. This runs inside the assembly worker's
worktree, which `build_inspiration.sh` has reset with `git read-tree -u --reset`
+ `git clean -fdxq` -- deleting the gitignored `.venv`. Resolving the workspace
project there would be slow on a cold base and can fail outright on an
unrelated build error, aborting a publish that is otherwise fine (the same
reasoning the assembly script's boot smoke-check documents). So this script and
its schema module are snapshotted out of the tree BEFORE that reset, exactly as
`scan_secrets.sh` and `betterleaks.toml` already are, and the schema module
imports only pydantic and the standard library.

Exit codes: 0 = valid; 1 = problems found (they are printed); 2 = usage error.
"""

import argparse
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

_SCHEMA_MODULE_NAME = "inspiration_manifest"
_IN_REPO_SCHEMA_PATH = Path(
    "system/services/env_converge/src/env_converge/inspiration_manifest.py"
)
_APT_RESOLVE_TIMEOUT_SECONDS = 120.0


class ValidateInspirationError(Exception):
    """Base exception for this script's own failures."""


class SchemaModuleNotFoundError(ValidateInspirationError, FileNotFoundError):
    """Raised when the shared schema module cannot be located."""

    def __init__(self, searched: tuple[Path, ...]) -> None:
        self.searched = searched
        super().__init__(
            "Cannot find inspiration_manifest.py (searched: "
            + ", ".join(str(path) for path in searched)
            + "). It is snapshotted next to this script by build_inspiration.sh; "
            "without it there is no validation and no fallback."
        )


class AptUnavailableError(ValidateInspirationError, OSError):
    """Raised when apt is absent but apt packages were declared.

    Mirrors the secret scan's no-fallback stance: the publish flow only ever
    runs inside the workspace container, so a missing apt is a broken
    environment, never a reason to ship unverified declarations.
    """

    def __init__(self) -> None:
        super().__init__(
            "apt-get is not available, but this inspiration declares apt packages. "
            "Declared packages cannot be verified against the pinned mirror here; "
            "run the publish flow inside the workspace container."
        )


def _load_schema_module(script_path: Path) -> ModuleType:
    """Load the shared schema module from the snapshot dir or this script's repo.

    Both candidates are relative to THIS SCRIPT, never to the tree being
    validated: the snapshot dir when `build_inspiration.sh` copied the pair out
    ahead of its reset, otherwise the checkout this script itself lives in. The
    validated tree is an assembled snapshot and does not necessarily carry a
    usable copy of the schema.
    """
    candidates = (
        script_path.parent / f"{_SCHEMA_MODULE_NAME}.py",
        script_path.parents[4] / _IN_REPO_SCHEMA_PATH,
    )
    for candidate in candidates:
        if not candidate.is_file():
            continue
        spec = importlib.util.spec_from_file_location(_SCHEMA_MODULE_NAME, candidate)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    raise SchemaModuleNotFoundError(candidates)


def check_apt_packages_resolve(packages: tuple[str, ...]) -> tuple[str, ...]:
    """Problems with apt packages that do not resolve at the pinned timestamp.

    The worktree already carries the snapshot-pinned sources written by
    write_apt_sources.sh, so this is a local index query -- no network write,
    nothing installed.
    """
    if not packages:
        return ()
    if shutil.which("apt-get") is None:
        raise AptUnavailableError()
    completed = subprocess.run(
        [
            "apt-get",
            "install",
            "--no-install-recommends",
            "--dry-run",
            "-qq",
            *packages,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=_APT_RESOLVE_TIMEOUT_SECONDS,
    )
    if completed.returncode == 0:
        return ()
    return (
        "declared apt packages do not resolve in the pinned snapshot mirror "
        f"({', '.join(packages)}); apt said: {completed.stderr.strip()[-500:]}",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "repo_root",
        nargs="?",
        default=".",
        help="Repo root holding inspiration.toml (default: the current directory)",
    )
    parser.add_argument(
        "--skip-apt-check",
        action="store_true",
        help="Skip apt resolution only (for schema-only checks off a Debian host)",
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    schema = _load_schema_module(Path(__file__).resolve())

    manifest_path = repo_root / schema.MANIFEST_TOML_NAME
    if not manifest_path.is_file():
        print(
            f"validate_inspiration: no {schema.MANIFEST_TOML_NAME} at {repo_root}",
            file=sys.stderr,
        )
        return 1

    try:
        problems = list(schema.validate_inspiration_tree(repo_root))
    except schema.InspirationManifestError as e:
        print(f"validate_inspiration: {e}", file=sys.stderr)
        return 1

    if not args.skip_apt_check:
        manifest = schema.load_inspiration_manifest(manifest_path)
        problems.extend(check_apt_packages_resolve(manifest.environment.apt))

    if problems:
        print(
            f"validate_inspiration: {len(problems)} problem(s) with the inspiration at {repo_root}:",
            file=sys.stderr,
        )
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print(f"validate_inspiration: {manifest_path.name} is valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
