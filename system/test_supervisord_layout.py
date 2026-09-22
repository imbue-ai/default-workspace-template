"""The supervisord config layout: every program lives in ``system/supervisord.conf.d/``.

Programs live one per file there, reached via the ``[include] files`` glob in
``system/supervisord.conf``. Several readers depend on that -- the OOM band
checks in ``system/services/oom_priority``, the ``build-app`` scaffolder's port
pre-flight and duplicate-name guard, ``migrate-workspace``'s port scan, and
(cross-repo) the minds evals evidence capture, which joins each registered app
to the program that supervises it. Neither ``configparser`` nor a plain ``cat``
follows supervisord's ``[include]``, so each of them reads that directory by
name rather than expanding the glob the config declares.

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
import importlib.util
import re
from pathlib import Path
from types import ModuleType

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


# Every in-repo reader of this config, by the path it lives at. They cannot share a helper: they
# sit in four separate uv workspace members, and two of them are standalone scripts. migrate-
# workspace's REMOTE reader lists the directory over SSH, in shell, so its own suite runs that
# shell against a local workspace instead.
_READER_PATHS: dict[str, Path] = {
    "scaffolder": _REPO_ROOT / ".agents/skills/build-app/scripts/scaffold_flask_lib.py",
    "migrate_workspace": _REPO_ROOT
    / ".agents/skills/migrate-workspace/scripts/migrate_workspace.py",
    "app_manifests": _REPO_ROOT / "system/test_app_manifests.py",
    "oom_bands": _REPO_ROOT
    / "system/services/oom_priority/bin/oom_tag_service_test.py",
}


def _load(name: str) -> ModuleType:
    """Import a reader by path, under its own module name so pytest's own copy is untouched."""
    spec = importlib.util.spec_from_file_location(
        "_supervisord_reader_" + name, _READER_PATHS[name]
    )
    assert spec is not None and spec.loader is not None, (
        f"cannot load {_READER_PATHS[name]}"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _declared_in(paths: list[Path]) -> set[str]:
    return {
        program for path in paths for program in _SECTION_RE.findall(path.read_text())
    }


def _program_names_in(paths: list[Path]) -> set[str]:
    return {
        name for path in paths for name in _PROGRAM_ONLY_RE.findall(path.read_text())
    }


def test_every_reader_sees_every_program_the_include_glob_reaches() -> None:
    """All four in-repo readers see the programs the config's own ``[include]`` glob reaches.

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
        _declared_in(_load("scaffolder")._supervisord_conf_files(_SUPERVISORD_CONF))
        == canonical
    )
    assert (
        _declared_in(_load("migrate_workspace")._local_supervisord_configs(_REPO_ROOT))
        == canonical
    )
    # These two return command-by-name rather than a file list; the names are the shared claim.
    # The band reader covers event listeners as well, the manifest reader programs only.
    assert set(_load("oom_bands")._command_by_supervisord_program()) == canonical
    assert set(_load("app_manifests")._command_by_program()) == _program_names_in(
        canonical_files
    )


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
