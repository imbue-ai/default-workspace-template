"""Print where the locked Playwright looks for each named browser's executable, one per line.

The env.d browser units run this under the root venv (``uv run python``) on every converge
to find the paths they link or install into. Each argument is a Playwright registry browser
name (``chromium``, ``chromium-headless-shell``), optionally followed by ``=<dir>`` to ask
where it would live under ``PLAYWRIGHT_BROWSERS_PATH=<dir>``. The answer comes from
Playwright's own registry, so it tracks the locked version's revision and per-architecture
layout. There is no public API for the headless shell's path, so this reads Playwright's
internal registry module: a path that cannot be resolved prints an empty line (and the
reason to stderr), so the output always has one line per argument.
"""

import argparse
import os
import pathlib
import subprocess
import sys

import playwright

_DRIVER_DIR = pathlib.Path(playwright.__file__).parent / "driver"
_REGISTRY_MODULE = _DRIVER_DIR / "package" / "lib" / "server" / "registry" / "index.js"
_EXECUTABLE_PATH_SCRIPT = (
    "const {registry} = require(process.argv[1]); "
    'process.stdout.write(registry.findExecutable(process.argv[2]).executablePath("python") || "")'
)


def resolve_executable_path(browser: str, browsers_path: str | None) -> str:
    """Where Playwright looks for ``browser``'s executable, under ``browsers_path`` if given."""
    env = dict(os.environ)
    if browsers_path:
        env["PLAYWRIGHT_BROWSERS_PATH"] = browsers_path
    result = subprocess.run(
        [
            str(_DRIVER_DIR / "node"),
            "-e",
            _EXECUTABLE_PATH_SCRIPT,
            str(_REGISTRY_MODULE),
            browser,
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print where the locked Playwright looks for each named browser's executable."
    )
    parser.add_argument("specs", nargs="+", metavar="BROWSER[=DIR]")
    args = parser.parse_args(argv)
    for spec in args.specs:
        browser, _, browsers_path = spec.partition("=")
        try:
            path = resolve_executable_path(browser, browsers_path or None)
        except (OSError, subprocess.CalledProcessError) as error:
            detail = (
                error.stderr.strip()
                if isinstance(error, subprocess.CalledProcessError)
                else ""
            )
            sys.stderr.write(f"could not resolve {spec}: {error} {detail}\n")
            path = ""
        sys.stdout.write(f"{path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
