"""Stand up a new Flask app and register it with the workspace.

Creates `system/apps/<package>/` with a Flask starter (synchronous; flask-sock
is available for WebSockets) and its `app.toml` manifest, as a standalone uv
project: its own `pyproject.toml`, a `uv.lock` seeded from the root lock (so the
app starts on the built-ins' versions and resolves from the warm cache), and a
`.venv` linked beside the uv cache.

It then registers the directory the way `app-install add` does
(`app_install.operations.add_app`): the app gets the lowest free port, its
record `system/config/apps/<name>.toml`, and its generated supervisord program
`system/supervisord.conf.d/<name>.conf`, whose command links and syncs that
project (`--locked`), registers the manifest, and execs the app's entry point
from the app's own `.venv/bin` with the port in its environment. The port is in
no file of the app's directory. A registration the workspace refuses (a name, a
program, or a port something else holds) removes the directory again, so a
refusal leaves nothing behind.

The app is not a root workspace member, so a dependency it adds changes only its
own lock and venv, never the chat's or another app's
(docs/system/blueprint/app-environments/). It reaches the app SDK
(`imbue.app_sdk`) through an editable path dependency on `app-schema`, which
depends on it and whose own workspace sources keep the SDK and `imbue-common`
on the root's mngr pin.

No shared file is written: the record and the program each live in a file of
the app's own, and the root `pyproject.toml` and `uv.lock` are not touched. That
is what lets two agents scaffold two apps concurrently without editing the same
file.

Usage:
    uv run --no-sync .agents/skills/build-app/python/build_app_skill/scaffold_flask_lib.py \\
        --name inbox-status --description "inbox status dashboard" \\
        --icon-file icon.svg [--display-name "Inbox status"] \\
        [--port 8081] [--extra-dep "jinja2>=3.1"] [--extra-dep "anthropic>=0.40"] \\
        [--secrets-file inbox --secrets-variable INBOX_API_KEY]

Run from the repo root (`/home/user/workspace`). Fails non-zero with a clear message on
any failure (lib already exists, reserved name, refused registration, sync failure, etc.).
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Callable, Iterable

from app_registration import forward_port
from workspace_bare_scripts import venv_links

# The template's own app and service names, which forward_port.py has no reason
# to know about: it refuses names that can never be an origin label, while these
# are perfectly good labels already taken by something built in. Both kebab and
# snake forms are listed so a kebab name that converts to a snake-cased existing
# name is also rejected. Everything the registry itself refuses -- reserved
# names, reserved prefixes, length, character set -- is asked of forward_port.py
# at validation time rather than copied here.
RESERVED_NAMES = frozenset(
    {
        "system-interface",
        "system_interface",
        "share-gateway",
        "share_gateway",
        "bootstrap",
        "github-sync",
        "host-backup",
        "terminal",
        "deferred-install",
        "imbue-common",
    }
)
KEBAB_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
# The <name> of data/.secrets/<name>.env, as the chat app's secret card and
# app_manifest.primitives.SECRET_FILE_NAME_PATTERN spell it.
SECRET_FILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
# A variable a secret file sets, as app_manifest.primitives.ENV_VAR_NAME_PATTERN spells it.
SECRET_VARIABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _kebab_to_snake(name: str) -> str:
    return name.replace("-", "_")


def _read_and_validate_icon(path: Path) -> str:
    # forward_port owns the registration rule (names and icons alike); reuse it so
    # a name it would refuse, or a bad icon, fails here instead of at registration.
    markup, error = forward_port.read_icon_file(path)
    if error is not None:
        sys.exit(f"error: {error}")
    assert markup is not None
    return markup


def _validate_name(name: str) -> None:
    # The name becomes the leading label of the service's origin hostname (the
    # app is served at http://<name>.<workspace-host>/), so the scaffold is
    # deliberately stricter than the registry: kebab-case only, letter-start, no
    # underscores, where forward_port.py still tolerates legacy names like
    # ``system_interface``. Strictness is all this adds. Whether the registry
    # would accept the name is asked of forward_port.py itself, so a name the
    # scaffold mints always registers -- rather than being pinned by a copy of
    # its reserved list that can drift out from under the check.
    if not KEBAB_RE.match(name):
        sys.exit(
            f"error: --name {name!r} is not valid kebab-case "
            "(lowercase letters/digits with single hyphens, "
            "starting with a letter)"
        )
    registration_problem = forward_port.validate_service_name(name)
    if registration_problem is not None:
        sys.exit(
            f"error: --name {name!r} could not be registered: {registration_problem}"
        )
    if name in RESERVED_NAMES or _kebab_to_snake(name) in RESERVED_NAMES:
        sys.exit(f"error: --name {name!r} is reserved")


def _validate_secrets(secrets_file: str | None, variables: list[str]) -> None:
    """Refuse, before anything is written, a secret declaration the manifest would refuse.

    The generated program loads data/.secrets/<file>.env through with_secrets.py from the
    manifest's ``[[secrets]]`` table, which names the file and at least one variable it sets.
    """
    if secrets_file is None:
        if variables:
            sys.exit(
                "error: --secrets-variable names a variable of a secret file; pass --secrets-file too"
            )
        return
    if not SECRET_FILE_NAME_RE.fullmatch(secrets_file):
        sys.exit(
            f"error: --secrets-file {secrets_file!r} must be lowercase letters, digits and "
            "hyphens, starting with a letter or digit "
            "(the <name> of data/.secrets/<name>.env)"
        )
    if not variables:
        sys.exit(
            f"error: --secrets-file {secrets_file!r} needs at least one --secrets-variable "
            "(a variable the file sets, such as EXAMPLE_API_KEY)"
        )
    for variable in variables:
        if not SECRET_VARIABLE_NAME_RE.fullmatch(variable):
            sys.exit(
                f"error: --secrets-variable {variable!r} must be letters, digits and "
                "underscores, not starting with a digit"
            )
    if len(set(variables)) != len(variables):
        sys.exit("error: --secrets-variable names a variable twice")


def _format_dep_list(extras: Iterable[str]) -> str:
    base = [
        '"app-schema"',
        '"flask>=3.0"',
        '"flask-sock>=0.7"',
        '"imbue-common"',
        '"loguru>=0.7"',
        '"pydantic>=2.0"',
        '"werkzeug>=3.0"',
    ]
    extras_lines = [f'"{dep}"' for dep in extras]
    all_lines = base + extras_lines
    return ",\n    ".join(all_lines)


def _lib_pyproject(name: str, package: str, description: str, extras: list[str]) -> str:
    deps_block = _format_dep_list(extras)
    return f"""[project]
name = "{name}"
version = "0.1.0"
description = {_toml_basic_string(description)}
readme = "README.md"
requires-python = ">=3.12"
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

[dependency-groups]
dev = [
    # The app's tests run from its own venv, which its sync gives the dev group: pytest
    # and what the ratchets check with.
    "imbue-common[testing]",
    # What runner_test.py asserts the API document with.
    "app-schema[testing]",
    # Used by test_no_type_errors in the ratchets, pinned to the version the shell and chat use.
    "ty==0.0.87",
    # The harden pass's browser tests: the repo-root venv and conftest do not reach this suite,
    # so its own venv carries Playwright (which drives Fortress by path) and the plugin behind
    # each browser test's pytest.mark.timeout marker.
    "playwright>=1.40",
    "pytest-timeout>=2.3.0",
]

# Makes this directory the tests' own pytest root, so they run against this app's venv
# rather than the workspace's root configuration.
[tool.pytest.ini_options]
testpaths = ["."]
# The test gate selects by these marks (AGENTS.md, "Manual verification and testing").
markers = [
    "browser: drives a browser",
    "frontend: reads the app's frontend source",
]

# Supply-chain cooldown, the workspace root's two-week window: a registry release is
# adopted once it has been public this long, so a compromised release can be noticed
# and yanked before a relock pulls it in.
[tool.uv]
exclude-newer = "14 days"

# CLEANUP: once imbue-app-sdk 0.1.0 is published to PyPI, depend on it directly: replace
# "app-schema" with "imbue-app-sdk>=0.1,<1" and "app-schema[testing]" with
# "imbue-app-sdk[testing]", drop this source entry, and add
# `exclude-newer-package = {{ imbue-app-sdk = false }}` under [tool.uv].
# Until then the app SDK (imbue.app_sdk) reaches this app through app-schema, which
# depends on it. app-schema's own workspace sources apply in this lock, so the SDK and
# imbue-common come from the commit the workspace pins mngr to; a source for either
# here as well would be a second URL for one package, which uv refuses to lock.
[tool.uv.sources]
app-schema = {{ path = "../../libs/app_schema", editable = true }}

# Makes this directory ty's project; without a [tool.ty] table ty keeps searching upward.
[tool.ty]
"""


def _lib_runner(
    name: str,
    package: str,
    description: str,
    display_name: str,
) -> str:
    env_var = f"{package.upper()}_DATA_DIR"
    port_env_var = f"{package.upper()}_PORT"
    host_env_var = f"{package.upper()}_HOST"
    # Python literals for the generated source: the document's title is the manifest's display name.
    title_literal = repr(display_name)
    description_literal = repr(description)
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
- Listen address: bind ``HOST`` and ``PORT`` (defined below), which are
  read from the ``{host_env_var}`` and ``{port_env_var}`` env vars. The
  workspace allocates this app's port and sets ``{port_env_var}`` in its
  program's environment, and an editing agent sets it to a *spare* port
  to boot a throwaway instance alongside the live one (see the
  update-app skill). No file of the app holds the port, so never
  hardcode one at the ``run_simple`` call or anywhere else.

This is a synchronous Flask app served by the threaded Werkzeug server.
The app owns its own browser origin (the forwarder routes
``http://{name}.<workspace-host>/`` straight to this port), so it serves
at ``/`` and root-absolute URLs, cookies, and service workers all work
unmodified -- nothing rewrites anything. Use ``flask_sock`` if you need
WebSockets.
"""

import base64
import hashlib
import os
from pathlib import Path

from flask import Flask, Response, request
from imbue.app_sdk.api_schema import (
    DEFAULT_API_SCHEMA_PATH,
    RouteModels,
    RouteTable,
    SchemalessRoute,
    register_api_schema_route,
)
from imbue.app_sdk.routes import model_response
from imbue.app_sdk.shell_modules import SHELL_MODULE_ROUTE_KEY, register_shell_module_routes
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import setup_logging
from loguru import logger
from pydantic import Field
from werkzeug.serving import run_simple

# Persistent state for this app lives under DATA_DIR. It defaults to
# ``data/.apps/{name}/`` but is overridable via the ``{env_var}`` env var
# so a throwaway instance can run against a *copy* of the data while editing --
# see the update-app skill. Always read/write state through DATA_DIR;
# never hardcode ``data/.apps/{name}/`` at a call site, or the override is
# bypassed. A writing call site should ``DATA_DIR.mkdir(parents=True,
# exist_ok=True)`` before writing.
DATA_DIR = Path(os.environ.get("{env_var}", "data/.apps/{name}"))

# Listen address, read from the environment. The workspace sets ``{port_env_var}``
# to the port it allocated for this app, and an editing agent sets it to a spare
# port to boot a throwaway instance next to the live one (see the update-app
# skill). With it unset (the app run on its own, outside a workspace) the
# operating system picks a free port. Never hardcode the port at the
# ``run_simple`` call, or the workspace's port is bypassed. The host is
# loopback unless ``{host_env_var}`` says otherwise: the forwarder reaches the
# app from inside the same container.
HOST = os.environ.get("{host_env_var}", "127.0.0.1")
PORT = int(os.environ.get("{port_env_var}", "0"))

# The script every page serves (keep it on every page): it connects the page to
# the shell, reports where the page is on the handshake so the shell can reopen
# this app's window at the same place, and installs the element context menu.
# A page visited outside the shell runs it harmlessly: nothing arrives, and the
# menu's Explain and Modify rows grey out.
SHELL_PAGE_SCRIPT_BODY = """
  import {{ connectToShell }} from "/_static/app_contract.js";
  import {{ installElementContextMenu }} from "/_static/context_menu.js";
  let handshake = null;
  const connection = connectToShell({{
    onHandshake: (received) => {{
      handshake = received;
      connection.location(location.pathname + location.search, document.title);
    }},
  }});
  installElementContextMenu({{ connection, handshake: () => handshake }});
"""
SHELL_PAGE_SCRIPT = f'<script type="module">{{SHELL_PAGE_SCRIPT_BODY}}</script>'


def inline_script_source(body: str) -> str:
    """The Content-Security-Policy source admitting one inline <script> whose body is exactly ``body``."""
    return "'sha256-" + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode() + "'"


# Every HTML page is served under this policy (the app is reachable from the
# internet once the workspace is shared): scripts only from this origin plus the
# inline blocks listed here by hash, fetches, sockets and images only from this
# origin, inline styles, frames of any http(s) page, no plugins. Anything else a
# page pulls in (a CDN script, Google Fonts, an image hosted elsewhere, a
# third-party API called from the browser) is blocked until its origin is added
# to the matching directive; a new inline <script> needs its
# ``inline_script_source`` added.
# The browser posts every block to ``/_csp-report`` below, which logs it on
# stderr (the supervisor log), so a blocked resource is never silent; the
# ``'report-sample'`` makes the browser include where a blocked inline script
# is and how it starts, since the report otherwise names it only as "inline".
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    f"script-src 'self' 'report-sample' {{inline_script_source(SHELL_PAGE_SCRIPT_BODY)}}; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; frame-src http: https:; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; "
    "report-uri /_csp-report"
)
BROWSER_EXTENSION_SCHEMES = ("chrome-extension:", "moz-extension:", "safari-web-extension:")


class HealthResponse(FrozenModel):
    """What the health probe answers."""

    status: str = Field(description="Always 'ok'")


# What every route exchanges, which the app's API document publishes and every parse and answer
# is checked against (build-app skill, "The app describes its own API"): a route that reads or
# answers JSON names its pydantic models here and uses ``parse_request_body`` and
# ``model_response``; a page, a file or a stream is declared schemaless with the reason.
ROUTE_TABLE: RouteTable = {{
    ("GET", "/"): SchemalessRoute(reason="the app's page"),
    ("GET", "/health"): RouteModels(response_model=HealthResponse),
    SHELL_MODULE_ROUTE_KEY: SchemalessRoute(reason="a browser-side module the shell builds"),
    ("POST", "/_csp-report"): SchemalessRoute(reason="the browser's report of what the page's policy blocked"),
}}

app = Flask("{package}", static_folder=None)
# The app describes its own API: an OpenAPI document of every route below, served at the
# manifest's api_schema_path.
register_api_schema_route(
    app, DEFAULT_API_SCHEMA_PATH, title={title_literal}, description={description_literal}, route_table=ROUTE_TABLE
)
# The browser-side modules the workspace shell builds, served from this app's
# own origin at ``/_static/``: the app contract (how a page talks to the shell
# framing it) and the element context menu. A module import is a fetch without
# cookies, which the forwarder refuses across origins, so the page cannot load
# them from the shell. The SDK fetches each from the running shell once; with no
# shell (the app run on its own) they are a 404 and the page runs without the
# shell's features.
register_shell_module_routes(app)


@app.after_request
def add_content_security_policy(response: Response) -> Response:
    if response.mimetype == "text/html":
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    return response


@app.route("/")
def index() -> Response:
    """The app's page."""
    return Response(
        "<!doctype html><html><head><title>{name}</title></head><body>"
        "<h1>{name}</h1>"
        "<p>{description}</p>"
        + SHELL_PAGE_SCRIPT
        + "</body></html>",
        mimetype="text/html",
    )


@app.route("/_csp-report", methods=["POST"])
def content_security_policy_report() -> Response:
    """Where the browser reports what the page's security policy blocked."""
    body = request.get_json(force=True, silent=True)
    report = body.get("csp-report") if isinstance(body, dict) else None
    if not isinstance(report, dict):
        return Response(status=204)
    blocked, page, source, directive, line, sample = (
        str(report.get(key, ""))[:500]
        for key in ("blocked-uri", "document-uri", "source-file", "effective-directive", "line-number", "script-sample")
    )
    # A block caused by a browser extension is the policy doing its job and is not logged.
    if blocked.startswith(BROWSER_EXTENSION_SCHEMES) or source.startswith(BROWSER_EXTENSION_SCHEMES):
        return Response(status=204)
    # An inline script or style is reported as "inline": say where it is and how it starts instead.
    where = f" at {{source}}:{{line}}" if source and line else ""
    starting = f" starting {{sample!r}}" if sample else ""
    remedy = (
        "add its inline_script_source to CONTENT_SECURITY_POLICY if the page needs it"
        if blocked == "inline"
        else "add its origin to CONTENT_SECURITY_POLICY if the page needs it"
    )
    logger.error(
        "Content-Security-Policy blocked {{!r}} on {{!r}} ({{}}){{}}{{}}; {{}}", blocked, page, directive, where, starting, remedy
    )
    return Response(status=204)


@app.route("/health")
def health() -> Response:
    """Whether the app is up."""
    return model_response(HealthResponse(status="ok"))


def main() -> None:
    setup_logging()
    run_simple(HOST, PORT, app, threaded=True, use_reloader=False, use_debugger=False)


if __name__ == "__main__":
    main()
'''


def _lib_ratchets() -> str:
    return """from pathlib import Path

from imbue.imbue_common.ratchet_testing import standard_ratchet_checks as rc
from imbue.imbue_common.ratchet_testing.ratchets import check_no_type_errors
from inline_snapshot import snapshot

_DIR = Path(__file__).parent


# Code safety


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


# Exception handling


def test_prevent_bare_except() -> None:
    rc.check_bare_except(_DIR, snapshot(0))


def test_prevent_broad_exception_catch() -> None:
    rc.check_broad_exception_catch(_DIR, snapshot(0))


def test_prevent_builtin_exception_raises() -> None:
    rc.check_builtin_exception_raises(_DIR, snapshot(0))


# Import style


def test_prevent_inline_imports() -> None:
    rc.check_inline_imports(_DIR, snapshot(0))


def test_prevent_relative_imports() -> None:
    rc.check_relative_imports(_DIR, snapshot(0))


# Banned libraries and patterns


def test_prevent_asyncio_import() -> None:
    rc.check_asyncio_import(_DIR, snapshot(0))


def test_prevent_dataclasses_import() -> None:
    rc.check_dataclasses_import(_DIR, snapshot(0))


# Types


def test_no_type_errors() -> None:
    check_no_type_errors(_DIR)
"""


def _lib_readme(name: str, description: str) -> str:
    return f"# {name}\n\n{description}\n"


# The app_manifest library's limit (app_manifest.primitives.MAX_DISPLAY_NAME_LENGTH).
# This script keeps the library (and the pydantic it loads) out of its imports;
# a drift test in scaffold_flask_lib_test.py keeps the two equal.
MAX_DISPLAY_NAME_LENGTH = 64
MAX_APP_DESCRIPTION_LENGTH = 200


def _toml_basic_string(value: str) -> str:
    """``value`` as a TOML basic string: the manifest's and the pyproject's ``description``."""
    # ensure_ascii=False: a TOML basic string takes raw non-ASCII, but refuses the surrogate
    # escapes json.dumps would otherwise write for a character outside the basic plane.
    return json.dumps(value, ensure_ascii=False)


def _check_description(description: str) -> str:
    """The manifest's ``description``, refused before anything is written when the manifest would refuse it."""
    if not description.strip():
        sys.exit("error: --description must not be empty")
    if "\n" in description or "\r" in description:
        sys.exit("error: --description is a single line")
    if len(description) > MAX_APP_DESCRIPTION_LENGTH:
        sys.exit(
            f"error: --description is over {MAX_APP_DESCRIPTION_LENGTH} characters ({len(description)}); "
            "it is the one line an agent reads to know what the app is for"
        )
    return description


def _display_name(description: str, explicit: str | None) -> str:
    """The manifest's ``display_name``: the explicit one, else the description when it fits."""
    candidate = explicit if explicit is not None else description
    candidate = candidate.strip()
    if not candidate:
        sys.exit(
            "error: the display name must not be empty (--display-name, or --description when it is omitted)"
        )
    if len(candidate) > MAX_DISPLAY_NAME_LENGTH:
        sys.exit(
            f"error: the display name {candidate!r} is over {MAX_DISPLAY_NAME_LENGTH} characters; "
            "pass a shorter --display-name (the description can stay long)"
        )
    if '"' in candidate or "\\" in candidate:
        sys.exit("error: the display name may not contain double quotes or backslashes")
    return candidate


def _lib_manifest(
    name: str,
    package: str,
    description: str,
    display_name: str,
    secrets_file: str | None,
    secrets_variables: list[str],
) -> str:
    manifest = _MANIFEST_TEMPLATE.format(
        name=name,
        display_name=display_name,
        description_toml=_toml_basic_string(description),
        package_upper=package.upper(),
    )
    if secrets_file is None:
        return manifest
    return manifest + _MANIFEST_SECRETS_TEMPLATE.format(
        file_toml=_toml_basic_string(secrets_file),
        variables_toml=", ".join(
            _toml_basic_string(variable) for variable in secrets_variables
        ),
    )


def _write_lib(
    repo_root: Path,
    name: str,
    description: str,
    display_name: str,
    extras: list[str],
    icon_markup: str,
    secrets_file: str | None,
    secrets_variables: list[str],
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
    (lib_dir / "app.toml").write_text(
        _lib_manifest(
            name, package, description, display_name, secrets_file, secrets_variables
        )
    )
    (lib_dir / "README.md").write_text(_lib_readme(name, description))
    (lib_dir / "icon.svg").write_text(icon_markup.strip() + "\n")
    (lib_dir / f"test_{package}_ratchets.py").write_text(_lib_ratchets())
    (src_dir / "__init__.py").write_text("")
    (src_dir / "runner.py").write_text(
        _lib_runner(name, package, description, display_name)
    )
    (src_dir / "runner_test.py").write_text(_lib_runner_test(package))
    return lib_dir


def _lib_runner_test(package: str) -> str:
    """The one test every scaffolded app starts with: its API document says what its manifest says."""
    return f"""from pathlib import Path

from imbue.app_sdk.app_testing import assert_api_schema_describes_app

from {package}.runner import app


def test_the_app_describes_its_api_the_way_its_manifest_says() -> None:
    assert_api_schema_describes_app(app.test_client(), Path(__file__).resolve().parents[2] / "app.toml")
"""


# The manifest (system/apps/<package>/app.toml; see system/libs/app_manifest).
# ``priority = "user"`` is what puts a user-built app in the user band: the
# generated program's ``oom_tag_service.py`` prefix names it. ``stop_when_no_windows``
# is stated outright so the rule an app runs under is in its own manifest. No
# launch paths: the shell offers ``open`` at the app's root. No
# ``default_shortcut``: the shell gives every desktop a shortcut of ``open``.
# The ``[preview]`` table is the library's default for the name spelled out,
# so an edit to the runner's env names has the table to keep in step beside it.
_MANIFEST_TEMPLATE = """\
name = "{name}"
display_name = "{display_name}"
# What the app is for, in the person's terms: what an agent reads to decide whether this app
# does what it needs. The app's API document (served at api_schema_path) carries it too.
description = {description_toml}
api_schema_path = "/api/schema"
icon = "icon.svg"
priority = "user"
program = "{name}"
# The shell stops the app once no window shows it (a minute after the last one
# closes) and starts it again on the next request; false keeps it running for
# the life of the workspace. Set false if the app does work between requests
# (a background thread, a poller, a subscription to an outside service, a job
# that outlives the window): a stop loses that work. Periodic work (a refresh
# every N minutes, a nightly sync) is a routine, not a loop in the service:
# see the manage-routines skill.
stop_when_no_windows = true

# How update-app boots a throwaway preview of this app: on a free port, over a
# scratch copy of its data (see .agents/skills/update-app/python/update_app_skill/preview_app.py).
[preview]
env = {{{package_upper}_PORT = "{{port:main}}", {package_upper}_HOST = "{{host}}", {package_upper}_DATA_DIR = "{{copy:data}}"}}
copies = {{data = "data/.apps/{name}"}}
"""

# The ``[[secrets]]`` table of an app scaffolded with ``--secrets-file``. The generated
# program wraps the entry point in with_secrets.py once per table, so the file's
# variables reach the app and appear in no config file and no command line.
_MANIFEST_SECRETS_TEMPLATE = """
# The secret file the app runs under (data/.secrets/<file>.env) and the variables it
# sets. Add a `note`: one line for someone installing the app, saying what the value
# is and where to get it.
[[secrets]]
file = {file_toml}
variables = [{variables_toml}]
"""


def _run_checked(argv: list[str], repo_root: Path, description: str) -> None:
    result = subprocess.run(argv, cwd=repo_root, capture_output=True, text=True)
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        sys.exit(f"error: `{description}` failed (exit {result.returncode})")


def _register_app(
    repo_root: Path,
    package: str,
    requested_port: int | None,
    is_port_bound: Callable[[int], bool],
) -> tuple[int, tuple[str, ...]]:
    """Register the app directory just written, as ``app-install add`` does.

    Returns the port the app was given and the repo-relative files written for it: its
    record first, then its generated program.

    The workspace refuses an app that would take a name, a program, a console script or
    a port something else holds. A refused app's directory is removed before exiting
    non-zero, so the refusal leaves nothing for the agent to clean up.
    """
    # Imported here, so only a scaffold that gets as far as registering pays for the
    # installer, the app_manifest library and their pydantic.
    from app_install.errors import AppInstallError, CollisionError
    from app_install.operations import add_app, install_lock
    from app_manifest.errors import AppManifestError

    with install_lock(repo_root):
        try:
            outcome = add_app(repo_root, package, requested_port, is_port_bound)
        except (AppInstallError, AppManifestError) as error:
            shutil.rmtree(repo_root / "system" / "apps" / package)
            problems = (
                error.problems if isinstance(error, CollisionError) else (str(error),)
            )
            sys.exit(
                f"error: the workspace refused to register the app, so system/apps/{package} "
                "was removed again:\n"
                + "\n".join(f"  - {problem}" for problem in problems)
            )
    return outcome.record.port, outcome.changed_paths


def _create_project_env(repo_root: Path, package: str) -> None:
    """Lock the new app on its own and build its linked venv.

    The lock starts as a copy of the root one, so `uv lock` keeps every version the
    built-ins run where the app needs the same package, and resolves from the warm
    cache; it then drops what the app does not use.
    """
    relative = f"system/apps/{package}"
    shutil.copyfile(repo_root / "uv.lock", repo_root / relative / "uv.lock")
    _run_checked(
        ["uv", "lock", "--project", relative],
        repo_root,
        f"uv lock --project {relative}",
    )
    venv_links.ensure(None, [repo_root / relative])
    _run_checked(
        ["uv", "sync", "--locked", "--project", relative],
        repo_root,
        f"uv sync --locked --project {relative}",
    )


def _start_and_wait(
    repo_root: Path, name: str, port: int, timeout: float = 10.0
) -> None:
    _run_checked(["supervisorctl", "reread"], repo_root, "supervisorctl reread")
    _run_checked(["supervisorctl", "update"], repo_root, "supervisorctl update")

    url = f"http://127.0.0.1:{port}/health"
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "scaffold-check"})
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                if resp.status == 200:
                    return
        except Exception as e:
            last_err = e
            time.sleep(0.05)

    status_res = subprocess.run(
        ["supervisorctl", "status", name], cwd=repo_root, capture_output=True, text=True
    )
    sys.exit(
        f"error: service {name} failed to become healthy at {url} within {timeout}s (last error: {last_err})\n"
        f"status: {status_res.stdout.strip()}"
    )


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
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--name", required=True, help="kebab-case app name")
    parser.add_argument("--description", required=True, help="one-line description")
    parser.add_argument(
        "--display-name",
        default=None,
        help="what users see for the app (the manifest's display_name, at most 64 characters); defaults to the description",
    )
    parser.add_argument(
        "--icon-file",
        required=True,
        help="the app's icon: an .svg file holding a single house-style <svg> (see the build-app skill)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="the loopback port to register the app on (the lowest free one if omitted)",
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
        "--secrets-file",
        default=None,
        help="the <name> of a data/.secrets/<name>.env the app needs (requested through the connect-external-service skill); declared in the manifest's [[secrets]], so the generated program runs under with_secrets.py and its variables reach the app",
    )
    parser.add_argument(
        "--secrets-variable",
        action="append",
        default=[],
        help="a variable the --secrets-file sets, such as EXAMPLE_API_KEY (repeatable; at least one with --secrets-file)",
    )
    parser.add_argument(
        "--skip-uv-sync",
        action="store_true",
        help="skip the app's lock and venv after generation (for tests/dry runs)",
    )
    parser.add_argument(
        "--start",
        action="store_true",
        help="have supervisord pick up the generated program (reread + update) and wait for health endpoint",
    )
    args = parser.parse_args()

    _validate_name(args.name)
    _validate_secrets(args.secrets_file, list(args.secrets_variable))
    icon_markup = _read_and_validate_icon(Path(args.icon_file))
    repo_root = (
        Path(args.repo_root).resolve()
        if args.repo_root
        else _find_repo_root(Path.cwd())
    )
    package = _kebab_to_snake(args.name)
    _check_description(args.description)
    display_name = _display_name(args.description, args.display_name)

    lib_dir = _write_lib(
        repo_root,
        args.name,
        args.description,
        display_name,
        list(args.extra_dep),
        icon_markup,
        args.secrets_file,
        list(args.secrets_variable),
    )
    # Imported here for the same reason as inside _register_app.
    from app_install.ports import is_port_bound

    port, registered_paths = _register_app(repo_root, package, args.port, is_port_bound)

    if not args.skip_uv_sync:
        _create_project_env(repo_root, package)

    if args.start:
        _start_and_wait(repo_root, args.name, port)

    start_msg = (
        f"Service `{args.name}` started and healthy on http://127.0.0.1:{port}/.\n"
        if args.start
        else ""
    )
    record_path, *generated_paths = registered_paths
    print(
        f"{start_msg}Created lib at {lib_dir.relative_to(repo_root)} "
        f"(app `{args.name}` on port {port}, recorded in {record_path}; the window "
        f"renders at the service's own origin, http://{args.name}.<workspace-host>/). "
        f"Generated, never edited by hand: {', '.join(generated_paths)}. To change how the "
        f"app runs, change {lib_dir.relative_to(repo_root)}/app.toml and run "
        f"`uv run --no-sync app-install regenerate {args.name}`. "
        f"Next: implement your routes in src/{package}/runner.py, then verify per "
        f"references/verify.md (verify_app.py or curl + Playwright against http://127.0.0.1:{port}/)."
    )


if __name__ == "__main__":
    main()
