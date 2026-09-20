"""Every built-in app manifest validates, names a real supervisord program, is what
that program's registration line passes, and declares a memory band that exists.

Every manifest in the tree -- user-built apps included -- also validates against the real
repo root, which is what checks that its declared references still exist where it says.
"""

import ast
import configparser
import re
import tomllib
from pathlib import Path

import pytest
from app_manifest.manifest import MANIFEST_FILENAME, load_manifest
from app_manifest.primitives import RESERVED_APP_NAMES
from app_manifest.scope import APP_CONVENTIONS
from app_manifest.scope import SKILL_CONVENTIONS
from oom_priority import bands

_REPO_ROOT = Path(__file__).resolve().parents[1]
_APPS_DIR = _REPO_ROOT / "system" / "apps"
_SUPERVISORD_CONF = _REPO_ROOT / "system" / "supervisord.conf"

_MANIFEST_FLAG = re.compile(r"--manifest\s+(\S+)")


# The apps the template ships. Only these are checked: a workspace built from the
# template may carry user-built apps (with a manifest whose priority is ``user``,
# or with no manifest at all), and this suite runs there too.
_BUILT_IN_APP_PACKAGES = ("browser", "chat", "files", "system_interface", "terminal", "terminal_pty")


def _built_in_manifest_paths() -> list[Path]:
    return [
        _APPS_DIR / package / MANIFEST_FILENAME for package in _BUILT_IN_APP_PACKAGES
    ]


def _every_manifest_path() -> list[Path]:
    """Every app.toml in the tree, user-built apps included; the suite runs inside workspaces."""
    return sorted(_APPS_DIR.glob(f"*/{MANIFEST_FILENAME}"))


def _command_by_program() -> dict[str, str]:
    """Every program the config declares: the main file, plus the drop-ins beside it.

    ``configparser`` does not follow supervisord's ``[include]``, and the template declares its
    programs one per file under ``system/supervisord.conf.d/``, so a bare read of the main config
    finds none of them. The drop-ins are read after the main config, which reproduces
    supervisord's precedence; ``test_supervisord_layout.py`` pins that directory as the one the
    include glob names.
    """
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(_SUPERVISORD_CONF)
    parser.read(
        sorted((_SUPERVISORD_CONF.parent / "supervisord.conf.d").glob("*.conf"))
    )
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

    A Python app runs its tool's console script and registers from inside it (the terminal's
    entry point calls ``app_manifest.registry.register_app`` with its manifest), so the manifest
    path is a constant the script's module exports as ``MANIFEST_PATH`` rather than a flag on the
    command line.
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


@pytest.mark.parametrize(
    "manifest_path", _every_manifest_path(), ids=lambda path: path.parent.name
)
def test_every_manifest_validates_against_the_repo_root(manifest_path: Path) -> None:
    # Loading with the real root is what checks the [[references]] location rules and that
    # every referenced artifact still exists, so a deleted skill or doc fails here rather
    # than silently leaving a review pass pointed at nothing.
    load_manifest(manifest_path, repo_root=_REPO_ROOT)


@pytest.mark.parametrize(
    "convention_path", sorted(set(APP_CONVENTIONS + SKILL_CONVENTIONS)), ids=str
)
def test_every_convention_doc_the_scope_file_names_exists(convention_path: str) -> None:
    # The scope file points every review pass at these; a renamed doc would otherwise
    # send them to nothing without a test noticing.
    assert (_REPO_ROOT / convention_path).is_file(), convention_path


def test_every_declared_wiring_program_has_a_supervisord_block() -> None:
    command_by_program = _command_by_program()
    for manifest_path in _every_manifest_path():
        for program in load_manifest(manifest_path, repo_root=_REPO_ROOT).wiring.programs:
            assert program in command_by_program, (
                f"{manifest_path} declares wiring program {program!r}, which supervisord.conf does not define"
            )


def test_the_first_label_of_every_standalone_program_is_a_reserved_app_name() -> None:
    # An app named after a standalone program's first label would claim that program as
    # its <name>-<role> sidecar when its footprint is computed, so the label is reserved.
    app_programs = {load_manifest(path, repo_root=_REPO_ROOT).program for path in _every_manifest_path()}
    standalone_labels = {
        program.partition("-")[0]
        for program in _command_by_program()
        if "-" in program and program not in app_programs
    }
    unreserved = sorted(standalone_labels - RESERVED_APP_NAMES)

    assert unreserved == [], f"add these to RESERVED_APP_NAMES (and forward_port.py's RESERVED_NAMES): {unreserved}"


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


def test_built_in_manifests_agree_with_the_contract_table() -> None:
    by_name = {
        manifest.name: manifest
        for manifest in map(load_manifest, _built_in_manifest_paths())
    }

    assert by_name["system_interface"].internal is True
    assert by_name["system_interface"].critical is True
    # The terminal's pty origin (desktop-interface contracts.md section 2): ttyd, framed by the
    # terminal's wrapper page, never offered on its own.
    assert by_name["terminal-pty"].internal is True
    assert by_name["terminal-pty"].critical is True
    assert by_name["terminal-pty"].program == "terminal-pty"
    assert by_name["terminal-pty"].priority == "terminal"
    assert by_name["terminal-pty"].launch_paths == ()
    assert by_name["terminal-pty"].default_shortcut is None
    assert by_name["chat"].internal is False
    assert by_name["chat"].critical is True
    assert by_name["chat"].program == "chat"
    assert by_name["chat"].priority == "chat"
    assert by_name["chat"].default_shortcut is not None
    assert by_name["chat"].default_shortcut.mode == "new"
    assert by_name["terminal"].critical is True
    assert by_name["files"].critical is False
    assert by_name["browser"].critical is False
    for name in ("terminal", "files", "browser"):
        assert by_name[name].default_shortcut is not None
        assert by_name[name].default_shortcut.mode == "focus"
    # The desktop interface's launch paths (desktop-interface contracts.md section 2).
    assert by_name["system_interface"].launch_paths == ()
    for name, launch_path in (("chat", "/new"), ("terminal", "/new"), ("files", "/"), ("browser", "/new")):
        assert [(entry.id, entry.path) for entry in by_name[name].launch_paths] == [("new", launch_path)], name
        assert by_name[name].default_shortcut is not None
        assert by_name[name].default_shortcut.launch == "new", name
    assert [param.name for param in by_name["chat"].launch_paths[0].params] == ["account_id", "message"]
    assert [param.name for param in by_name["terminal"].launch_paths[0].params] == ["workdir"]
    assert [param.name for param in by_name["files"].launch_paths[0].params] == ["path"]
    assert [param.name for param in by_name["browser"].launch_paths[0].params] == ["url"]
