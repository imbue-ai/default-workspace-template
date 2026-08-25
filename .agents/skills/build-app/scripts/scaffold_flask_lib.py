#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["tomlkit>=0.12"]
# ///
"""Stand up a new Flask app (and its supervisord program entry).

Creates `system/apps/<package>/` with a Flask starter (synchronous; flask-sock
is available for WebSockets), updates the root pyproject.toml
sources/dependencies (the `system/apps/*` member glob picks the package up
automatically), appends a `[program:<name>]` block to
system/supervisord.conf, and runs `uv sync --all-packages` to materialize the
workspace.

Usage:
    uv run .agents/skills/build-app/scripts/scaffold_flask_lib.py \\
        --name inbox-status --description "inbox status dashboard" \\
        --icon-file /tmp/inbox-status-icon.svg \\
        [--port 8081] [--extra-dep "jinja2>=3.1"] [--extra-dep "anthropic>=0.40"]

`--icon-file` is required: draw the app's icon (house style: a single
`<svg viewBox="0 0 24 24">` of monochrome line art, stroke='currentColor',
fill='none') before scaffolding. Its contents are copied to
`system/apps/<package>/icon.svg`, which the generated supervisord command
registers via `forward_port.py --icon-file` on every start -- so a later
icon edit takes effect on the next restart.

Run from the repo root (`/home/user/workspace`). Fails non-zero with a clear message on
any failure (lib already exists, reserved name, invalid icon, sync failure, etc.).
"""

import argparse
import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable

import tomlkit
from tomlkit import TOMLDocument
from tomlkit.items import Array, Table

# Both kebab and snake forms are reserved so a kebab name that converts to
# a snake-cased existing app or service name is also rejected.
RESERVED_NAMES = frozenset(
    {
        "system-interface",
        "system_interface",
        "share-gateway",
        "share_gateway",
        "app-watcher",
        "bootstrap",
        "github-sync",
        "host-backup",
        "terminal",
        "deferred-install",
        "imbue-common",
        # forward_port.py rejects ``localhost`` at registration time (it is
        # the local origin's root domain); reserve it here too so the scaffold
        # never mints an app that cannot register.
        "localhost",
        # ``auth`` is reserved for the share stack's dedicated ``auth-<rand>``
        # origin label (the sole public ``/_auth/*`` origin); forward_port.py
        # rejects it, so the scaffold must too.
        "auth",
    }
)
# Workspace hostnames carry their coordinate as a ``host-<hex>`` label
# (``agent-`` is the legacy spelling); a service name starting with either
# prefix could collide with that coordinate label, so forward_port.py rejects
# both and the scaffold must too.
RESERVED_NAME_PREFIXES = ("host-", "agent-")
LOWEST_AUTO_PORT = 8080
KEBAB_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
LOCALHOST_PORT_RE = re.compile(r"http://(?:localhost|127\.0\.0\.1):(\d+)")

# forward_port.py owns icon reading and validation (.svg files only, a single
# safe <svg>, length cap); load it from this checkout so the scaffold rejects a
# bad icon up front instead of the app failing to register on its first
# supervisord start.
_FORWARD_PORT_PATH = (
    Path(__file__).resolve().parents[4] / "system" / "scripts" / "forward_port.py"
)


def _read_and_validate_icon(path: Path) -> str:
    spec = importlib.util.spec_from_file_location("_forward_port_for_scaffold", _FORWARD_PORT_PATH)
    if spec is None or spec.loader is None:
        sys.exit(f"error: could not load {_FORWARD_PORT_PATH} for icon validation")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    markup, read_error = module.read_icon_file(path)
    if read_error is not None:
        sys.exit(f"error: {read_error}")
    icon_error = module.validate_icon(markup)
    if icon_error is not None:
        sys.exit(f"error: {icon_error}")
    return markup


def _kebab_to_snake(name: str) -> str:
    return name.replace("-", "_")


def _validate_name(name: str) -> None:
    # The name becomes the leading label of the service's origin hostname
    # (the app is served at http://<name>.<workspace-host>/), so it must be
    # DNS-safe kebab-case and stay out of the reserved coordinate prefix
    # space. forward_port.py accepts a superset (underscores are tolerated
    # there for legacy names like ``system_interface``), so every name the
    # scaffold mints registers cleanly -- a drift test in
    # system/scripts/forward_port_test.py pins that subset relation.
    if not KEBAB_RE.match(name):
        sys.exit(
            f"error: --name {name!r} is not valid kebab-case "
            "(lowercase letters/digits with single hyphens, "
            "starting with a letter)"
        )
    for prefix in RESERVED_NAME_PREFIXES:
        if name.startswith(prefix):
            sys.exit(
                f"error: --name {name!r} starts with {prefix!r}, which is "
                "reserved for workspace hostnames"
            )
    if name in RESERVED_NAMES or _kebab_to_snake(name) in RESERVED_NAMES:
        sys.exit(f"error: --name {name!r} is reserved")


def _supervisord_conf_ports(supervisord_conf: Path) -> set[int]:
    # Every app registers its localhost backend via a forward_port.py call in
    # its [program:*] command, so scanning the whole config text for
    # http://localhost:<port> / http://127.0.0.1:<port> finds all in-use ports.
    if not supervisord_conf.exists():
        return set()
    text = supervisord_conf.read_text()
    return {int(match.group(1)) for match in LOCALHOST_PORT_RE.finditer(text)}


def _apps_toml_ports(apps_toml: Path) -> set[int]:
    if not apps_toml.exists():
        return set()
    doc = tomlkit.parse(apps_toml.read_text())
    apps = doc.get("apps", [])
    ports: set[int] = set()
    for app in apps:
        url = app.get("url", "")
        match = LOCALHOST_PORT_RE.search(str(url))
        if match:
            ports.add(int(match.group(1)))
    return ports


def _pick_port(repo_root: Path, requested: int | None) -> int:
    in_use = _supervisord_conf_ports(
        repo_root / "system/supervisord.conf"
    ) | _apps_toml_ports(repo_root / "data" / ".state" / "apps.toml")
    if requested is not None:
        if requested in in_use:
            sys.exit(
                f"error: --port {requested} is already in use by another app or service"
            )
        return requested
    port = LOWEST_AUTO_PORT
    while port in in_use:
        port += 1
    return port


def _format_dep_list(extras: Iterable[str]) -> str:
    base = ['"flask>=3.0"', '"flask-sock>=0.7"', '"werkzeug>=3.0"']
    extras_lines = [f'"{dep}"' for dep in extras]
    all_lines = base + extras_lines
    return ",\n    ".join(all_lines)


def _lib_pyproject(name: str, package: str, description: str, extras: list[str]) -> str:
    deps_block = _format_dep_list(extras)
    return f"""[project]
name = "{name}"
version = "0.1.0"
description = "{description}"
readme = "README.md"
requires-python = ">=3.11"
dependencies = [
    {deps_block},
]

[project.scripts]
{name} = "{package}.runner:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/{package}"]
"""


def _lib_runner(name: str, package: str, description: str, port: int) -> str:
    env_var = f"{package.upper()}_DATA_DIR"
    port_env_var = f"{package.upper()}_PORT"
    return f'''"""{description}.

Services run from /home/user/workspace (the repo root). Conventions:

- Persistent state (anything written and read across runs -- cursors,
  caches, snapshots, user records): read and write it under ``DATA_DIR``
  (defined below), never a hardcoded ``data/.apps/{name}/`` at the
  call site. ``DATA_DIR`` defaults to ``data/.apps/{name}/`` but
  honors the ``{env_var}`` env var, so an editing agent can point a
  throwaway instance at a *copy* of the data instead of the live store
  (see the update-app skill). Do NOT use ``Path(__file__)``-based
  paths for state -- the bug to avoid is one process writing to
  ``/home/user/workspace/data/.apps/...`` while another reads from
  ``/home/user/workspace/system/apps/<pkg>/data/...``.
- Static assets shipped alongside this file (templates, default
  configs, bundled JSON): ``Path(__file__).parent / "assets/..."`` is
  fine and is the right pattern.
- Listen port: bind ``PORT`` (defined below), which defaults to this
  app's assigned port but honors the ``{port_env_var}`` env var, so
  an editing agent can boot a throwaway instance on a *spare* port
  alongside the live one (see the update-app skill). Never hardcode
  the port at the ``run_simple`` call.

This is a synchronous Flask app served by the threaded Werkzeug server.
The app owns its own browser origin (the forwarder routes
``http://{name}.<workspace-host>/`` straight to this port), so it serves
at ``/`` and root-absolute URLs, cookies, and service workers all work
unmodified -- nothing rewrites anything. Use ``flask_sock`` if you need
WebSockets.
"""

import os
from pathlib import Path

from flask import Flask, Response
from werkzeug.serving import run_simple

# Persistent state for this app lives under DATA_DIR. It defaults to
# ``data/.apps/{name}/`` but is overridable via the ``{env_var}`` env var
# so a throwaway instance can run against a *copy* of the data while editing --
# see the update-app skill. Always read/write state through DATA_DIR;
# never hardcode ``data/.apps/{name}/`` at a call site, or the override is
# bypassed. A writing call site should ``DATA_DIR.mkdir(parents=True,
# exist_ok=True)`` before writing.
DATA_DIR = Path(os.environ.get("{env_var}", "data/.apps/{name}"))

# Listen port. Defaults to this app's assigned port but is overridable via
# the ``{port_env_var}`` env var so an editing agent can boot a throwaway
# instance on a spare port next to the live one (see the update-app skill).
# Never hardcode the port at the ``run_simple`` call, or the override is bypassed.
PORT = int(os.environ.get("{port_env_var}", "{port}"))

app = Flask("{package}", static_folder=None)


@app.route("/")
def index() -> Response:
    # The location beacon: post the path being viewed one hop up (to the
    # workspace shell embedding this page) on each page load, so the shell can
    # reopen this app's tab at the same place. Keep the line on every page you
    # serve; the shell validates the sender's origin and ignores the rest.
    return Response(
        "<!doctype html><html><body>"
        "<h1>{name}</h1>"
        "<p>{description}</p>"
        "<script>if (window.parent !== window) window.parent.postMessage("
        '{{type: "minds-location", path: location.pathname + location.search}}, "*");</script>'
        "</body></html>",
        mimetype="text/html",
    )


@app.route("/health")
def health() -> Response:
    return Response('{{"status": "ok"}}', mimetype="application/json")


def main() -> None:
    run_simple(
        "127.0.0.1", PORT, app, threaded=True, use_reloader=False, use_debugger=False
    )


if __name__ == "__main__":
    main()
'''


def _lib_ratchets() -> str:
    return """from pathlib import Path

from imbue.imbue_common.ratchet_testing import standard_ratchet_checks as rc
from inline_snapshot import snapshot

_DIR = Path(__file__).parent


# --- Code safety ---


def test_prevent_todos() -> None:
    rc.check_todos(_DIR, snapshot(0))


def test_prevent_exec_usage() -> None:
    rc.check_exec(_DIR, snapshot(0))


def test_prevent_eval_usage() -> None:
    rc.check_eval(_DIR, snapshot(0))


def test_prevent_while_true() -> None:
    rc.check_while_true(_DIR, snapshot(0))


def test_prevent_time_sleep() -> None:
    rc.check_time_sleep(_DIR, snapshot(0))


def test_prevent_global_keyword() -> None:
    rc.check_global_keyword(_DIR, snapshot(0))


def test_prevent_bare_print() -> None:
    rc.check_bare_print(_DIR, snapshot(0))


# --- Exception handling ---


def test_prevent_bare_except() -> None:
    rc.check_bare_except(_DIR, snapshot(0))


def test_prevent_broad_exception_catch() -> None:
    rc.check_broad_exception_catch(_DIR, snapshot(0))


def test_prevent_builtin_exception_raises() -> None:
    rc.check_builtin_exception_raises(_DIR, snapshot(0))


# --- Import style ---


def test_prevent_inline_imports() -> None:
    rc.check_inline_imports(_DIR, snapshot(0))


def test_prevent_relative_imports() -> None:
    rc.check_relative_imports(_DIR, snapshot(0))


# --- Banned libraries and patterns ---


def test_prevent_asyncio_import() -> None:
    rc.check_asyncio_import(_DIR, snapshot(0))


def test_prevent_dataclasses_import() -> None:
    rc.check_dataclasses_import(_DIR, snapshot(0))

"""


def _lib_readme(name: str, description: str) -> str:
    return f"# {name}\n\n{description}\n"


def _write_lib(
    repo_root: Path,
    name: str,
    description: str,
    port: int,
    extras: list[str],
    icon_markup: str,
) -> Path:
    package = _kebab_to_snake(name)
    lib_dir = repo_root / "system" / "apps" / package
    if lib_dir.exists():
        sys.exit(f"error: {lib_dir} already exists")
    src_dir = lib_dir / "src" / package
    src_dir.mkdir(parents=True)
    (lib_dir / "pyproject.toml").write_text(
        _lib_pyproject(name, package, description, extras)
    )
    (lib_dir / "README.md").write_text(_lib_readme(name, description))
    (lib_dir / "icon.svg").write_text(icon_markup.strip() + "\n")
    (lib_dir / f"test_{package}_ratchets.py").write_text(_lib_ratchets())
    (src_dir / "__init__.py").write_text("")
    (src_dir / "runner.py").write_text(_lib_runner(name, package, description, port))
    return lib_dir


def _ensure_in_array(array: Array, value: str) -> bool:
    """Append value to a TOML array if missing. Returns True if appended."""
    for item in array:
        if str(item) == value:
            return False
    array.append(value)
    return True


def _update_root_pyproject(repo_root: Path, name: str, package: str) -> None:
    path = repo_root / "pyproject.toml"
    doc: TOMLDocument = tomlkit.parse(path.read_text())

    project = doc.get("project")
    if not isinstance(project, Table):
        sys.exit("error: root pyproject.toml is missing a [project] table")
    deps = project.get("dependencies")
    if not isinstance(deps, Array):
        sys.exit(
            "error: root pyproject.toml [project].dependencies is missing or not an array"
        )
    _ensure_in_array(deps, name)

    tool = doc.get("tool")
    if not isinstance(tool, Table):
        sys.exit("error: root pyproject.toml is missing a [tool] table")
    uv = tool.get("uv")
    if not isinstance(uv, Table):
        sys.exit("error: root pyproject.toml is missing [tool.uv]")
    workspace = uv.get("workspace")
    if not isinstance(workspace, Table):
        sys.exit("error: root pyproject.toml is missing [tool.uv.workspace]")
    # No members edit needed: the root pyproject's "system/apps/*" member glob
    # already covers every package under system/apps/.
    sources = uv.get("sources")
    if not isinstance(sources, Table):
        sys.exit("error: root pyproject.toml is missing [tool.uv.sources]")
    if name not in sources:
        source_entry = tomlkit.inline_table()
        source_entry["workspace"] = True
        sources[name] = source_entry

    path.write_text(tomlkit.dumps(doc))


_SUPERVISORD_PROGRAM_TEMPLATE = """\
[program:{name}]
command=python3 system/services/oom_priority/bin/oom_tag_service.py user bash -c "python3 system/scripts/forward_port.py --url http://localhost:{port} --name {name} --icon-file system/apps/{package}/icon.svg --program {name} && uv run {name}"
directory=/home/user/workspace
autostart=true
autorestart=true
startretries=1000000
stopasgroup=true
killasgroup=true
stdout_logfile=/var/log/supervisor/{name}-stdout.log
stderr_logfile=/var/log/supervisor/{name}-stderr.log
stdout_logfile_maxbytes=10MB
stderr_logfile_maxbytes=10MB
stdout_logfile_backups=3
stderr_logfile_backups=3
"""


def _update_supervisord_conf(repo_root: Path, name: str, package: str, port: int) -> None:
    # system/supervisord.conf is INI (not TOML) and has hand-written comments worth
    # preserving, so append a [program:<name>] block as text rather than
    # round-tripping through a parser. The command is wrapped in `bash -c "..."`
    # because supervisord exec's commands directly (no shell) and this one chains
    # forward_port.py with `&&`; the `oom_tag_service.py user` prefix tags the
    # new (user-created) app so it is shed before any built-in app or service
    # under memory pressure (see system/services/oom_priority/README.md).
    path = repo_root / "system/supervisord.conf"
    if not path.exists():
        sys.exit(f"error: {path} not found (cannot register the new app)")
    existing = path.read_text()
    if f"[program:{name}]" in existing:
        sys.exit(
            f"error: system/supervisord.conf already has a [program:{name}] section"
        )
    block = _SUPERVISORD_PROGRAM_TEMPLATE.format(name=name, package=package, port=port)
    path.write_text(existing.rstrip("\n") + "\n\n" + block)


def _run_uv_sync(repo_root: Path) -> None:
    result = subprocess.run(
        ["uv", "sync", "--all-packages"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        sys.exit(f"error: `uv sync --all-packages` failed (exit {result.returncode})")


def _find_repo_root(start: Path) -> Path:
    current = start.resolve()
    for parent in [current, *current.parents]:
        if (parent / "pyproject.toml").exists() and (
            parent / "system/supervisord.conf"
        ).exists():
            return parent
    sys.exit(
        "error: could not locate repo root (pyproject.toml + system/supervisord.conf)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--name", required=True, help="kebab-case app name")
    parser.add_argument("--description", required=True, help="one-line description")
    parser.add_argument(
        "--icon-file",
        required=True,
        help=(
            "path to the app's icon, an .svg file holding a single "
            "<svg viewBox=\"0 0 24 24\"> of monochrome line art "
            "(stroke='currentColor', fill='none'); copied to "
            "system/apps/<package>/icon.svg and registered on every start. "
            "SVG only -- no raster formats"
        ),
    )
    parser.add_argument(
        "--port", type=int, default=None, help="explicit port (auto-picked if omitted)"
    )
    parser.add_argument(
        "--extra-dep",
        action="append",
        default=[],
        help="additional pip dep beyond flask/flask-sock (repeatable)",
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="repo root (defaults to nearest ancestor containing pyproject.toml + system/supervisord.conf)",
    )
    parser.add_argument(
        "--skip-uv-sync",
        action="store_true",
        help="skip running `uv sync --all-packages` after generation (for tests/dry runs)",
    )
    args = parser.parse_args()

    _validate_name(args.name)
    icon_markup = _read_and_validate_icon(Path(args.icon_file))
    repo_root = (
        Path(args.repo_root).resolve()
        if args.repo_root
        else _find_repo_root(Path.cwd())
    )
    package = _kebab_to_snake(args.name)
    port = _pick_port(repo_root, args.port)

    lib_dir = _write_lib(
        repo_root, args.name, args.description, port, list(args.extra_dep), icon_markup
    )
    _update_root_pyproject(repo_root, args.name, package)
    _update_supervisord_conf(repo_root, args.name, package, port)

    if not args.skip_uv_sync:
        _run_uv_sync(repo_root)

    print(
        f"Created lib at {lib_dir.relative_to(repo_root)} "
        f"(app `{args.name}` on port {port}; the tab renders at the service's "
        f"own origin, http://{args.name}.<workspace-host>/). "
        f"Next: implement your routes in src/{package}/runner.py, then verify per "
        f"references/verify.md (curl + Playwright against http://127.0.0.1:{port}/)."
    )


if __name__ == "__main__":
    main()
