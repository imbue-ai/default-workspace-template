"""The supervisord config layout: every program is reachable through the declared globs.

Programs live one per file under ``system/supervisord.conf.d/``, reached via the
``[include] files`` glob in ``system/supervisord.conf``. Several readers depend
on that -- the OOM band checks in ``system/services/oom_priority``, the
``build-app`` scaffolder's port pre-flight and duplicate-name guard,
``migrate-workspace``'s port scan, and (cross-repo) the minds recovery probe.
All of them use ``configparser`` plus a hand-rolled expansion of the glob,
because ``configparser`` does not follow supervisord's ``[include]``.

That makes the glob a real contract, and one that fails *open*: a reader that
misses the drop-ins still parses a valid config, just a nearly empty one, and
its assertions pass over a single program. These tests pin the contract so that
degradation is loud.
"""

from __future__ import annotations

import configparser
import glob
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SUPERVISORD_CONF = _REPO_ROOT / "system" / "supervisord.conf"
_DROPIN_DIR = _REPO_ROOT / "system" / "supervisord.conf.d"

# system_interface stays in the main config until the minds recovery probe's
# include-aware read has shipped in a release; see the note in supervisord.conf.
_MAIN_CONFIG_PROGRAMS = frozenset({"system_interface"})

_SECTION_RE = re.compile(r"^\[(?:program|eventlistener):([^\]]+)\]", re.MULTILINE)


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

    This is the read every consumer performs. If it ever returns just
    ``system_interface``, a consumer is silently asserting over one program out
    of thirteen -- which is exactly how the OOM band checks degraded when the
    drop-ins were introduced.
    """
    parser = _parse_main_config()
    discovered = {
        section.partition(":")[2]
        for section in parser.sections()
        if section.startswith(("program:", "eventlistener:"))
    }
    for path in _expand_include_patterns(parser):
        discovered.update(_SECTION_RE.findall(path.read_text()))

    expected = {p.stem for p in _DROPIN_DIR.glob("*.conf")} | set(_MAIN_CONFIG_PROGRAMS)
    assert discovered == expected, (
        f"programs reachable through the include glob ({sorted(discovered)}) do not "
        f"match the drop-in files plus the main config's own programs ({sorted(expected)})"
    )
    assert len(discovered) > len(_MAIN_CONFIG_PROGRAMS), (
        "only the main config's own programs were discovered -- the [include] "
        "expansion is not finding the drop-ins"
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
    assert not duplicates, f"programs declared in more than one config file: {duplicates}"


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
