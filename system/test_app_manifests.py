"""Every built-in app manifest validates, names a real supervisord program, is what
that program's registration line passes, and declares a memory band that exists.
"""

import ast
import configparser
import glob
import re
import tomllib
from pathlib import Path

import pytest
from app_manifest.manifest import MANIFEST_FILENAME, load_manifest
from app_manifest.primitives import loopback_url_port
from oom_priority import bands

_REPO_ROOT = Path(__file__).resolve().parents[1]
_APPS_DIR = _REPO_ROOT / "system" / "apps"
_SUPERVISORD_CONF = _REPO_ROOT / "system" / "supervisord.conf"

_MANIFEST_FLAG = re.compile(r"--manifest\s+(\S+)")
_LOOPBACK_PORT_RE = re.compile(r"http://(?:localhost|127\.0\.0\.1):(\d+)")


def _port_constants(source: str) -> set[int]:
    """Every ``*_PORT = <int>`` an app's module declares, read from its source."""
    ports: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.AnnAssign) and node.value is not None:
            targets: list[ast.expr] = [node.target]
        elif isinstance(node, ast.Assign):
            targets = list(node.targets)
        else:
            continue
        if not any(
            isinstance(target, ast.Name) and target.id.endswith("_PORT")
            for target in targets
        ):
            continue
        for literal in ast.walk(node.value):
            if isinstance(literal, ast.Constant) and isinstance(literal.value, int):
                ports.add(literal.value)
    return ports


# The apps the template ships. Only these are checked: a workspace built from the
# template may carry user-built apps (with a manifest whose priority is ``user``,
# or with no manifest at all), and this suite runs there too.
_BUILT_IN_APP_PACKAGES = ("browser", "chat", "files", "system_interface", "terminal")


def _built_in_manifest_paths() -> list[Path]:
    return [
        _APPS_DIR / package / MANIFEST_FILENAME for package in _BUILT_IN_APP_PACKAGES
    ]


def _command_by_program() -> dict[str, str]:
    """Every program the config declares: the main file, plus the drop-ins its globs pull in.

    ``configparser`` does not follow supervisord's ``[include]`` directive, and the template
    declares its programs one per file under ``system/supervisord.conf.d/``, so a bare read of
    the main config finds almost none of them. The globs are expanded the way supervisord
    expands them -- against the directory of the config declaring them, with ``%(here)s``
    substituted -- and read after the main config, which reproduces supervisord's precedence.
    """
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(_SUPERVISORD_CONF)
    conf_dir = _SUPERVISORD_CONF.parent
    for pattern in (parser.get("include", "files", fallback="") or "").split():
        expanded = str(conf_dir / pattern.replace("%(here)s", str(conf_dir)))
        parser.read(sorted(glob.glob(expanded)))
    return {
        section.partition(":")[2]: parser[section].get("command", "")
        for section in parser.sections()
        if section.startswith("program:")
    }


def _manifest_path_constant(module_file: Path) -> str | None:
    """The ``MANIFEST_PATH = Path("...")`` an entry-point module declares, read from its source.

    Read rather than imported: an app's entry point pulls in its whole backend (the chat's loads
    mngr's configuration on import), which the root test environment does not set up.
    """
    for node in ast.walk(ast.parse(module_file.read_text())):
        if isinstance(node, ast.AnnAssign) and node.value is not None:
            target: ast.expr = node.target
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        else:
            continue
        if not (isinstance(target, ast.Name) and target.id == "MANIFEST_PATH"):
            continue
        for literal in ast.walk(node.value):
            if isinstance(literal, ast.Constant) and isinstance(literal.value, str):
                return literal.value
    return None


def _entry_point_manifest_paths(command: str) -> list[str]:
    """The manifest an app's own entry point registers with, when the program's command ends in one.

    A Python app runs its tool's console script and registers from inside it (the terminal calls
    the sidecar launcher with its manifest), so the manifest path is a constant the script's
    module exports as ``MANIFEST_PATH`` rather than a flag on the command line.
    """
    script_name = command.split()[-1]
    manifest_paths: list[str] = []
    for pyproject_path in _APPS_DIR.glob("*/pyproject.toml"):
        scripts = (
            tomllib.loads(pyproject_path.read_text())
            .get("project", {})
            .get("scripts", {})
        )
        if script_name not in scripts:
            continue
        module_name = scripts[script_name].partition(":")[0]
        module_relative = Path(*module_name.split(".")).with_suffix(".py")
        for module_file in (
            pyproject_path.parent / module_relative,
            pyproject_path.parent / "src" / module_relative,
        ):
            if module_file.is_file():
                manifest_path = _manifest_path_constant(module_file)
                if manifest_path is not None:
                    manifest_paths.append(manifest_path)
                break
    return manifest_paths


def test_every_built_in_app_directory_ships_a_manifest() -> None:
    # Every built-in app describes itself, whatever runs it.
    missing = [
        str(path.relative_to(_REPO_ROOT))
        for path in _built_in_manifest_paths()
        if not path.is_file()
    ]
    assert missing == [], f"built-in apps without an {MANIFEST_FILENAME}: {missing}"


@pytest.mark.parametrize(
    "manifest_path", _built_in_manifest_paths(), ids=lambda path: path.parent.name
)
def test_built_in_manifest_validates_and_matches_its_program(
    manifest_path: Path,
) -> None:
    manifest = load_manifest(manifest_path)
    command_by_program = _command_by_program()

    assert manifest.program in command_by_program, (
        f"{manifest_path} names program {manifest.program!r}, which supervisord.conf does not define"
    )
    # The registration is either a --manifest flag in the program's own command, in
    # a launcher script the command runs, or the MANIFEST_PATH constant of the app
    # entry point the command ends in.
    command = command_by_program[manifest.program]
    registration_sources = [command]
    for token in command.split():
        candidate = _REPO_ROOT / token
        if token.endswith(".sh") and candidate.is_file():
            registration_sources.append(candidate.read_text())
    manifest_flags = [
        match.group(1).strip('"')
        for source in registration_sources
        for match in _MANIFEST_FLAG.finditer(source)
    ] + _entry_point_manifest_paths(command)
    expected_relative = str(manifest_path.relative_to(_REPO_ROOT))
    # A launcher script may pass the manifest as "$REPO_ROOT/<relative path>", so an
    # absolute-looking flag counts when it ends with the repo-relative path.
    assert any(
        flag == expected_relative or flag.endswith(f"/{expected_relative}")
        for flag in manifest_flags
    ), (
        f"program {manifest.program!r} does not register with --manifest {expected_relative}: {manifest_flags}"
    )


@pytest.mark.parametrize(
    "manifest_path", _built_in_manifest_paths(), ids=lambda path: path.parent.name
)
def test_built_in_manifest_priority_is_a_band(manifest_path: Path) -> None:
    manifest = load_manifest(manifest_path)

    assert manifest.priority in bands.SERVICE_BANDS, (
        f"{manifest_path} declares priority {manifest.priority!r}, which is not a SERVICE_BANDS key"
    )
    # A built-in never sits in the user band: that would shed it before every
    # user-created app.
    assert manifest.priority != "user"


def test_every_built_in_app_declares_the_port_it_serves() -> None:
    """Every built-in manifest names its own ``url``.

    The manifests are what a reader that never starts an app consults for the ports the
    workspace holds -- the build-app scaffolder's port pre-flight and migrate-workspace's
    port scan both read them, and for an app whose supervisord command names no port
    because it registers itself at runtime they are the only static record. One app that
    stops declaring its url is one port those readers hand out twice.
    """
    undeclared = [
        manifest.name
        for manifest in map(load_manifest, _built_in_manifest_paths())
        if manifest.url is None
    ]
    assert not undeclared, (
        f"built-in apps whose manifest declares no url: {undeclared}. Add "
        'url = "http://localhost:<port>" -- without it the port pre-flight cannot see '
        "the port this app holds and will hand it to a new app."
    )


def test_no_two_built_in_apps_declare_the_same_port() -> None:
    """No port is claimed twice across the built-in manifests.

    Two apps on one port is a bind failure and a supervisord crash loop for whichever
    starts second, and nothing upstream of this catches it: ``forward_port.py`` upserts
    by name and never compares ports.
    """
    holders: dict[int, list[str]] = {}
    for manifest in map(load_manifest, _built_in_manifest_paths()):
        for url in (manifest.url, manifest.instances_url):
            if url is not None:
                holders.setdefault(loopback_url_port(url), []).append(manifest.name)
    shared = {port: names for port, names in holders.items() if len(names) > 1}
    assert not shared, f"ports declared by more than one built-in app: {shared}"


def test_a_built_in_app_names_no_port_its_manifest_does_not_declare() -> None:
    """An app's own source holds no port beyond the ones its manifest declares.

    The manifest is where an app's port is written; a second copy in the source is a
    copy that can drift, and the drift is silent until the app registers one port and
    binds another. Both spellings count: a loopback URL literal and a ``*_PORT`` integer
    constant.
    """
    stray: dict[str, set[int]] = {}
    for package in _BUILT_IN_APP_PACKAGES:
        manifest = load_manifest(_APPS_DIR / package / MANIFEST_FILENAME)
        declared = {
            loopback_url_port(url)
            for url in (manifest.url, manifest.instances_url)
            if url is not None
        }
        found: set[int] = set()
        for source in (_APPS_DIR / package).rglob("*.py"):
            # Test infrastructure names ports freely: fixture origins, and the
            # deliberately-dead ``http://localhost:1`` the conftests point at.
            if (
                source.name in ("conftest.py", "testing.py")
                or source.name.endswith("_test.py")
                or source.name.startswith("test_")
            ):
                continue
            text = source.read_text()
            found.update(int(match) for match in _LOOPBACK_PORT_RE.findall(text))
            found.update(_port_constants(text))
        if found - declared:
            stray[manifest.name] = found - declared
    assert not stray, (
        f"ports named in an app's source that its manifest does not declare: {stray}. "
        "Read the port from the manifest instead, or declare it there."
    )


def test_built_in_manifests_agree_with_the_contract_table() -> None:
    by_name = {
        manifest.name: manifest
        for manifest in map(load_manifest, _built_in_manifest_paths())
    }

    assert by_name["system_interface"].internal is True
    assert by_name["system_interface"].critical is True
    assert by_name["chat"].internal is False
    assert by_name["chat"].critical is True
    assert by_name["chat"].program == "chat"
    assert by_name["chat"].priority == "chat"
    assert by_name["chat"].instances is True
    assert by_name["chat"].instances_url is None
    assert by_name["chat"].default_shortcut is not None
    assert by_name["chat"].default_shortcut.action == "new"
    assert by_name["chat"].default_shortcut.mode == "new"
    assert [action.id for action in by_name["chat"].actions] == ["new", "subagent"]
    assert by_name["terminal"].critical is True
    assert by_name["files"].critical is False
    assert by_name["browser"].critical is False
    for name in ("terminal", "files", "browser"):
        assert by_name[name].instances is True
        assert by_name[name].default_shortcut is not None
        assert by_name[name].default_shortcut.action == "new"
        assert [action.id for action in by_name[name].actions] == ["new"]
    assert by_name["terminal"].instances_url == "http://127.0.0.1:7682"
    assert by_name["files"].instances_url == "http://127.0.0.1:8301"
    assert by_name["browser"].instances_url is None
