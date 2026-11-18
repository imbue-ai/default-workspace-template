"""The supervisord config layout: every program lives in ``system/supervisord.conf.d/``.

Programs live one per file there, reached via the ``[include] files`` glob in
``system/supervisord.conf``. Several readers depend on that -- the OOM band
checks in ``system/services/oom_priority``, the ``build-app`` scaffolder's port
pre-flight and duplicate-name guard, ``migrate-workspace``'s port scan,
``app_manifest.scope``'s wiring lookup, and (cross-repo) the minds evals
evidence capture, which joins each registered app to the program that supervises
it. Neither ``configparser`` nor a plain ``cat`` follows supervisord's
``[include]``, so each of them reads that directory by name rather than
expanding the glob the config declares.

That makes the directory a convention the config has to keep, and one that
fails *open*: if the include glob and the readers ever named different places, a
reader would still parse a valid config, just an empty one, and its assertions
would pass over nothing at all. These tests pin the two together so that
degradation is loud. This file is the one reader that expands the declared glob,
because checking the convention is its job.
"""

from __future__ import annotations

import configparser
import glob
import re
from pathlib import Path

import pytest
from app_manifest import scope
from build_app_skill import scaffold_flask_lib
from imbue.chat.config import DEFAULT_CHAT_PORT
from imbue.chat.config import Config as ChatConfig
from imbue.system_interface.config import Config as SystemInterfaceConfig
from migrate_workspace_skill import migrate_workspace
from oom_priority.supervisord_testing import supervisord_commands
from workspace_bare_scripts import start_after_first_wave

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SUPERVISORD_CONF = _REPO_ROOT / "system" / "supervisord.conf"
_DROPIN_DIR = _REPO_ROOT / "system" / "supervisord.conf.d"

# supervisord.conf declares no programs of its own: every one lives in a
# drop-in. Kept as a named empty set so a future carve-out has an obvious home
# and the assertions below stay readable.
_MAIN_CONFIG_PROGRAMS: frozenset[str] = frozenset()

_SECTION_RE = re.compile(r"^\[(?:program|eventlistener):([^\]]+)\]", re.MULTILINE)
# The manifest reader looks at ``[program:*]`` alone, so parity with it is stated in those terms.
_PROGRAM_ONLY_RE = re.compile(r"^\[program:([^\]]+)\]", re.MULTILINE)


def _expand_include_patterns(parser: configparser.ConfigParser) -> list[Path]:
    """The files the main config's ``[include] files`` globs match, in read order."""
    conf_dir = _SUPERVISORD_CONF.parent
    matched: list[Path] = []
    for pattern in (parser.get("include", "files", fallback="") or "").split():
        # supervisord joins each pattern to the directory of the config
        # declaring it, and expands %(here)s to that same directory.
        expanded = str(conf_dir / pattern.replace("%(here)s", str(conf_dir)))
        matched.extend(Path(p) for p in sorted(glob.glob(expanded)))
    return matched


def _parse_main_config() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(_SUPERVISORD_CONF)
    return parser


def _programs_declared_in(parser: configparser.ConfigParser) -> set[str]:
    """The program and event-listener names a parsed config declares itself.

    Its ``[include]``\\ s are not followed, so this is what that one file says.
    """
    return {
        section.partition(":")[2]
        for section in parser.sections()
        if section.startswith(("program:", "eventlistener:"))
    }


def _declared_in(paths: list[Path]) -> set[str]:
    return {
        program for path in paths for program in _SECTION_RE.findall(path.read_text())
    }


def _program_names_in(paths: list[Path]) -> set[str]:
    return {
        name for path in paths for name in _PROGRAM_ONLY_RE.findall(path.read_text())
    }


def test_every_reader_sees_every_program_the_include_glob_reaches() -> None:
    """Every in-repo reader sees the programs the config's own ``[include]`` glob reaches.

    The readers name ``supervisord.conf.d/`` outright; this file expands the glob the config
    declares. If the two ever come apart -- the glob moved, a reader's spelling drifted -- the
    reader returns a smaller set, or an empty one, and goes on asserting over it. Nothing else
    compares them; the other tests here each check the layout against itself.
    """
    canonical_files = [_SUPERVISORD_CONF] + _expand_include_patterns(
        _parse_main_config()
    )
    canonical = _declared_in(canonical_files)
    assert canonical, (
        "no programs found at all -- the fixture, not the readers, is wrong"
    )

    assert (
        _declared_in(scaffold_flask_lib._supervisord_conf_files(_SUPERVISORD_CONF))
        == canonical
    )
    # migrate-workspace's REMOTE reader lists the directory over SSH, in shell, so its own suite
    # runs that shell against a local workspace instead.
    assert (
        _declared_in(migrate_workspace._local_supervisord_configs(_REPO_ROOT))
        == canonical
    )
    # An empty read is this reader's ordinary answer ("the app is not registered yet"), so a
    # drift here is silent wherever else it would be loud.
    scope_files = [
        _REPO_ROOT / path for path in scope._supervisord_conf_paths(_REPO_ROOT)
    ]
    assert _declared_in(scope_files) == canonical
    # The band and manifest tests' reader returns command-by-name rather than a file list; the
    # names are the shared claim. The band tests read event listeners as well, the manifest tests
    # programs only.
    assert (
        set(supervisord_commands(_SUPERVISORD_CONF, ("program", "eventlistener")))
        == canonical
    )
    assert set(
        supervisord_commands(_SUPERVISORD_CONF, ("program",))
    ) == _program_names_in(canonical_files)


def test_include_glob_matches_every_dropin_file() -> None:
    """Every file in supervisord.conf.d/ is reached by the config's own glob.

    A drop-in the glob does not match is dead config: supervisord never reads
    it, so the program simply does not run, with nothing failing.
    """
    parser = _parse_main_config()
    matched = {p.resolve() for p in _expand_include_patterns(parser)}
    on_disk = {p.resolve() for p in _DROPIN_DIR.glob("*.conf")}
    assert on_disk, f"no drop-ins found in {_DROPIN_DIR}"
    unmatched = sorted(str(p.relative_to(_REPO_ROOT)) for p in on_disk - matched)
    assert not unmatched, (
        "drop-ins that the [include] glob does not match (supervisord will "
        f"never read them): {unmatched}"
    )


def test_every_program_is_discoverable_through_the_include_glob() -> None:
    """Reading the main config plus its globs finds every declared program.

    This is the read every consumer performs. If the glob expansion is wrong it
    returns whatever the main config declares on its own -- now nothing -- so a
    consumer silently asserts over an empty set. That is how the OOM band checks
    degraded when the drop-ins were introduced, when the main config still held
    ``system_interface`` and made the emptiness look like one real program.
    """
    parser = _parse_main_config()
    discovered = _programs_declared_in(parser)
    for path in _expand_include_patterns(parser):
        discovered.update(_SECTION_RE.findall(path.read_text()))

    expected = {p.stem for p in _DROPIN_DIR.glob("*.conf")} | set(_MAIN_CONFIG_PROGRAMS)
    assert discovered == expected, (
        f"programs reachable through the include glob ({sorted(discovered)}) do not "
        f"match the drop-in files plus the main config's own programs ({sorted(expected)})"
    )


def test_no_program_is_declared_twice() -> None:
    """A program declared in two files silently resolves to whichever is read last."""
    parser = _parse_main_config()
    seen: dict[str, str] = {}
    duplicates: list[str] = []
    sources = [(_SUPERVISORD_CONF, _SUPERVISORD_CONF.read_text())]
    sources += [(p, p.read_text()) for p in _expand_include_patterns(parser)]
    for path, text in sources:
        for program in _SECTION_RE.findall(text):
            rel = str(path.relative_to(_REPO_ROOT))
            if program in seen:
                duplicates.append(f"{program} (in {seen[program]} and {rel})")
            else:
                seen[program] = rel
    assert not duplicates, (
        f"programs declared in more than one config file: {duplicates}"
    )


def test_dropin_filename_matches_the_program_it_declares() -> None:
    """One program per file, named after it -- what the scaffolder and teardown assume.

    ``build-app``'s cleanup deletes ``supervisord.conf.d/<name>.conf`` by name,
    and the scaffolder refuses a name any existing drop-in declares. Both are
    wrong if a file's name and its program's name can diverge.
    """
    mismatched: list[str] = []
    for path in sorted(_DROPIN_DIR.glob("*.conf")):
        programs = _SECTION_RE.findall(path.read_text())
        if programs != [path.stem]:
            mismatched.append(f"{path.name} declares {programs}")
    assert not mismatched, (
        f"drop-ins whose filename does not match their single program: {mismatched}"
    )


def test_every_program_tolerates_settings_newer_than_its_mngr() -> None:
    """supervisord hands every program ``MNGR_ALLOW_UNKNOWN_CONFIG``.

    An update makes the new tree's ``.mngr/settings.toml`` live before it reinstalls mngr, and
    the old mngr rejects keys only the new one knows. The daemon's own ``environment`` is the one
    place every program inherits from, so the tolerance has to sit there.
    """
    environment = _parse_main_config().get("supervisord", "environment", fallback="")
    entries = [entry.strip() for entry in environment.split(",")]
    assert 'MNGR_ALLOW_UNKNOWN_CONFIG="1"' in entries, (
        f"[supervisord] environment does not set MNGR_ALLOW_UNKNOWN_CONFIG: {environment!r}"
    )


# The programs that start the moment supervisord does: the shell and the chat app, what they need
# to serve the desktop, the provider chooser and a sign-in, share-gateway (relay visitors and the
# owner's web access reach the shell through it), and the OOM machinery that has to be running
# before anything else allocates. Every other built-in program is the second wave.
_FIRST_WAVE_PROGRAMS: frozenset[str] = frozenset(
    {
        "system_interface",
        "chat",
        "agent-observer",
        "owner-exec",
        "vm-exec-register",
        "share-gateway",
        "earlyoom",
        "oom-tag-backstop",
        "publish_cgroup_meminfo",
    }
)

_GATE = "system/scripts/workspace_bare_scripts/start_after_first_wave.py"
_TAG_PREFIX = re.compile(r"^python3 \S*oom_tag_service\.py (\S+) ")


def _autostarted_built_in_commands() -> dict[str, str]:
    """Every program and event listener supervisord starts at boot, less the user-created ones (tagged ``user``)."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(_SUPERVISORD_CONF)
    parser.read(sorted(_DROPIN_DIR.glob("*.conf")))
    commands: dict[str, str] = {}
    for section in parser.sections():
        if not section.startswith(("program:", "eventlistener:")):
            continue
        if not parser[section].getboolean("autostart", fallback=True):
            continue
        command = parser[section].get("command", "")
        tag = _TAG_PREFIX.match(command)
        if tag is not None and tag.group(1) == "user":
            continue
        commands[section.partition(":")[2]] = command
    return commands


def test_every_built_in_program_starts_in_exactly_one_wave() -> None:
    """A built-in program is in the first wave or runs through the gate, never both and never neither.

    A new built-in program left ungated would cost the shell and the chat CPU at every boot; a
    first-wave program behind the gate would wait on itself for the gate's whole window.
    """
    commands = _autostarted_built_in_commands()
    assert _FIRST_WAVE_PROGRAMS <= set(commands), (
        f"first-wave programs that supervisord does not autostart: {sorted(_FIRST_WAVE_PROGRAMS - set(commands))}"
    )
    gated_first_wave = sorted(p for p in _FIRST_WAVE_PROGRAMS if _GATE in commands[p])
    assert not gated_first_wave, (
        f"first-wave programs held by the gate: {gated_first_wave}"
    )
    ungated = sorted(
        p
        for p, c in commands.items()
        if p not in _FIRST_WAVE_PROGRAMS and _GATE not in c
    )
    assert not ungated, (
        f"built-in programs in neither wave (prefix their command with {_GATE}): {ungated}"
    )


def test_the_gate_runs_inside_the_memory_band_wrapper() -> None:
    """The gate is the program's process until it execs, so the band has to be set before it, not after."""
    misplaced = sorted(
        program
        for program, command in _autostarted_built_in_commands().items()
        if _GATE in command
        and "oom_tag_service.py" in command
        and command.index("oom_tag_service.py") > command.index(_GATE)
    )
    assert not misplaced, (
        f"programs whose gate runs before their band is set: {misplaced}"
    )


def test_the_gate_waits_on_the_ports_and_pid_file_the_config_declares() -> None:
    shell_command = supervisord_commands(_SUPERVISORD_CONF, ("program",))[
        "system_interface"
    ]
    assert (
        f"--url http://localhost:{start_after_first_wave.SHELL_HEALTH[0]}"
        in shell_command
    )
    assert start_after_first_wave.CHAT_HEALTH[0] == DEFAULT_CHAT_PORT
    pidfile = _parse_main_config().get("supervisord", "pidfile")
    assert Path(pidfile) == start_after_first_wave.SUPERVISORD_PIDFILE


_LISTEN = "system/scripts/workspace_bare_scripts/listen_then_exec.py"


@pytest.mark.parametrize(
    ("program", "address"),
    [
        (
            "system_interface",
            f"{SystemInterfaceConfig.model_fields['system_interface_host'].default}:{SystemInterfaceConfig.model_fields['system_interface_port'].default}",
        ),
        (
            "chat",
            f"{ChatConfig.model_fields['chat_host'].default}:{ChatConfig.model_fields['chat_port'].default}",
        ),
    ],
)
def test_the_first_wave_apps_listen_then_register_and_announce_before_they_import(
    program: str, address: str
) -> None:
    """The shell and the chat bind the address they serve on before anything else, then register with
    ``--announce``, and only then exec the app: a request routed to them waits instead of being refused."""
    command = supervisord_commands(_SUPERVISORD_CONF, ("program",))[program]
    assert f"{_LISTEN} {address} " in command, command
    registration = command.index("forward_port.py")
    assert command.index(_LISTEN) < registration < command.index("exec .venv/bin/")
    assert "--announce" in command[registration : command.index("exec .venv/bin/")]
