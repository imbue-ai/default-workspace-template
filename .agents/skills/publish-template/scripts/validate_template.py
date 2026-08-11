#!/usr/bin/env python3
"""Validate the template assembled at a repo root. The publish flow's gate.

Checks, in one command:

  - `template.toml` parses and satisfies the schema;
  - `template.md` and `template.toml` agree (front matter, and the
    activation half of the requirements);
  - the thumbnail exists and is not still the generated placeholder;
  - no `<!-- FILL-IN (publishing agent)` block was left unreplaced anywhere;
  - every declared env.d unit is well-named and actually ships in the snapshot;
  - every declared apt package resolves in the mirrored universe at the
    publisher's pinned snapshot timestamp.

That last check is the reason unmirrorable third-party packages are rejected
here rather than at some adopter's first boot.

Run from the repo root being validated:

    uv run --no-project --with 'pydantic>=2' python validate_template.py [REPO_ROOT]

`--no-project` is load-bearing. This runs inside the assembly worker's
worktree, which `build_template.sh` has reset with `git read-tree -u --reset`
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

_SCHEMA_MODULE_NAME = "template_manifest"
_IN_REPO_SCHEMA_PATH = Path(
    "system/services/env_converge/src/env_converge/template_manifest.py"
)
_APT_RESOLVE_TIMEOUT_SECONDS = 120.0


class ValidateTemplateError(Exception):
    """Base exception for this script's own failures."""


class SchemaModuleNotFoundError(ValidateTemplateError, FileNotFoundError):
    """Raised when the shared schema module cannot be located."""

    def __init__(self, searched: tuple[Path, ...]) -> None:
        self.searched = searched
        super().__init__(
            "Cannot find template_manifest.py (searched: "
            + ", ".join(str(path) for path in searched)
            + "). It is snapshotted next to this script by build_template.sh; "
            "without it there is no validation and no fallback."
        )


class AptUnavailableError(ValidateTemplateError, OSError):
    """Raised when apt is absent but apt packages were declared.

    Mirrors the secret scan's no-fallback stance: the publish flow only ever
    runs inside the workspace container, so a missing apt is a broken
    environment, never a reason to ship unverified declarations.
    """

    def __init__(self) -> None:
        super().__init__(
            "apt-get is not available, but this template declares apt packages. "
            "Declared packages cannot be verified against the pinned mirror here; "
            "run the publish flow inside the workspace container."
        )


def _schema_module_candidates(script_path: Path) -> tuple[Path, ...]:
    """Where the schema module might be, relative to THIS SCRIPT.

    Never relative to the tree being validated: that is an assembled snapshot
    and does not necessarily carry a usable copy of the schema.

    The sibling comes first -- that is the copy `build_template.sh` snapshots
    out of the worktree ahead of its reset, and in that mode the script lives in
    a shallow mktemp dir like `/tmp/tmp.XXXXXX/`. Walking every ancestor for the
    in-repo path (rather than indexing a fixed number of levels up) is what
    keeps that case working: a fixed `parents[4]` raises IndexError on a path
    that shallow, and it did -- failing every real publish while passing tests
    that happened to run from a deeply-nested directory.
    """
    candidates = [script_path.parent / f"{_SCHEMA_MODULE_NAME}.py"]
    candidates.extend(
        ancestor / _IN_REPO_SCHEMA_PATH for ancestor in script_path.parents
    )
    return tuple(candidates)


def _load_schema_module(script_path: Path) -> ModuleType:
    """Load the shared schema module from the snapshot dir or this script's repo."""
    candidates = _schema_module_candidates(script_path)
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
        help="Repo root holding template.toml (default: the current directory)",
    )
    parser.add_argument(
        "--skip-apt-check",
        action="store_true",
        help="Skip apt resolution only (for schema-only checks off a Debian host)",
    )
    parser.add_argument(
        "--allow-unfinished",
        action="store_true",
        help=(
            "Permit FILL-IN blocks and the placeholder thumbnail. Only for the "
            "check build_template.sh runs on the skeleton it just generated; "
            "never for the worker's or the lead's pre-push run."
        ),
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    schema = _load_schema_module(Path(__file__).resolve())

    manifest_path = repo_root / schema.MANIFEST_TOML_NAME
    if not manifest_path.is_file():
        print(
            f"validate_template: no {schema.MANIFEST_TOML_NAME} at {repo_root}",
            file=sys.stderr,
        )
        return 1

    try:
        problems = list(
            schema.validate_template_tree(
                repo_root, is_unfinished_allowed=args.allow_unfinished
            )
        )
    except schema.TemplateManifestError as e:
        print(f"validate_template: {e}", file=sys.stderr)
        return 1

    if not args.skip_apt_check:
        manifest = schema.load_template_manifest(manifest_path)
        problems.extend(check_apt_packages_resolve(manifest.environment.apt))

    if problems:
        print(
            f"validate_template: {len(problems)} problem(s) with the template at {repo_root}:",
            file=sys.stderr,
        )
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print(f"validate_template: {manifest_path.name} is valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
