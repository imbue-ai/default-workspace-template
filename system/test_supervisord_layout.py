"""The supervisord config layout: every program is reachable through the declared globs.

Programs live one per file under ``system/supervisord.conf.d/``, reached via the
``[include] files`` glob in ``system/supervisord.conf``. Several readers depend
on that -- the OOM band checks in ``system/services/oom_priority``, the
``build-app`` scaffolder's port pre-flight and duplicate-name guard,
``migrate-workspace``'s port scan, and (cross-repo) the minds recovery probe.
All of them use ``configparser`` plus a hand-rolled expansion of the glob,
because ``configparser`` does not follow supervisord's ``[include]``.

That makes the glob a real contract, and one that fails *open*: a reader that
misses the drop-ins still parses a valid config, just an empty one, and its
assertions pass over nothing at all. These tests pin the contract so that
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

# supervisord.conf declares no programs of its own: every one lives in a
# drop-in. Kept as a named empty set so a future carve-out has an obvious home
# and the assertions below stay readable.
_MAIN_CONFIG_PROGRAMS: frozenset[str] = frozenset()

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


def _programs_declared_in(parser: configparser.ConfigParser) -> set[str]:
    """The program and event-listener names a parsed config declares itself.

    Its ``[include]``\\ s are not followed, so this is what that one file says.
    """
    return {
        section.partition(":")[2]
        for section in parser.sections()
        if section.startswith(("program:", "eventlistener:"))
    }


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


def test_a_program_free_main_config_implies_an_include_aware_vendored_probe() -> None:
    """The cross-repo release gate, as a check this repo's CI can actually see.

    The minds desktop client's recovery probe reads ``system/supervisord.conf``
    with ``configparser`` to source the system interface's inner port. An
    include-blind probe meeting a main config that declares no programs finds no
    port, degrades its port-listening and curl checks to UNKNOWN, and
    misdiagnoses a healthy workspace as unresponsive.

    Provisioning is tag-pinned and ``update-self`` is ceilinged to the running
    app's template ref, so landing this on ``main`` harms nobody. What binds is
    the release cut: a ``minds-v<N>`` template tag must not carry a program-free
    main config unless the mngr commit tagged ``minds-v<N>`` carries the
    include-aware probe. ``system/vendor/mngr`` is synced as part of that same
    release, so the vendored copy is the artifact this repo can check.

    This is deliberately a conditional: it says nothing while a program is
    declared in the main config, and becomes a permanent regression guard once
    the vendored probe follows the globs.
    """
    if _programs_declared_in(_parse_main_config()):
        return

    vendored_mngr = _REPO_ROOT / "system/vendor/mngr"
    if not vendored_mngr.is_dir():
        return

    # A missing probe is a failure, not a pass: the path lives in another repo's
    # tree, so an upstream rename would otherwise retire this gate silently --
    # leaving a program-free main config unguarded, which is the one outcome it
    # exists to prevent.
    vendored_probe = (
        vendored_mngr / "apps/minds/imbue/minds/desktop_client/recovery_probe_script.txt"
    )
    assert vendored_probe.is_file(), (
        f"{vendored_probe.relative_to(_REPO_ROOT)} is not in the vendored mngr subtree, "
        "so the release gate below cannot read the recovery probe. If the probe moved "
        "upstream, re-point this test at its new path -- do not drop the check."
    )

    source = vendored_probe.read_text()
    # The mechanism, not the word: the probe must read the [include] files
    # setting and glob its patterns, which is what following the directive means.
    follows_includes = 'parser.get("include"' in source and "glob.glob(" in source
    assert follows_includes, (
        f"{vendored_probe.relative_to(_REPO_ROOT)} does not follow supervisord's "
        "[include] globs, but system/supervisord.conf declares no programs -- so "
        "that probe would find no system_interface program and report the "
        "workspace unresponsive.\n\n"
        "This is the release gate, not a broken test. To satisfy it: land "
        "imbue-ai/mngr-internal#171 (the include-aware probe), then re-sync "
        "system/vendor/mngr. Do NOT relax this assertion -- the alternative is "
        "shipping a template tag that misdiagnoses every workspace on the "
        "matching minds release."
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
