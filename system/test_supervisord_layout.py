"""The supervisord config layout: every program is reachable through the declared globs.

Programs live one per file under ``system/supervisord.conf.d/``, reached via the
``[include] files`` glob in ``system/supervisord.conf``. Several readers depend
on that -- the OOM band checks in ``system/services/oom_priority``, the
``build-app`` scaffolder's port pre-flight and duplicate-name guard,
``migrate-workspace``'s port scan, and (cross-repo) the minds evals evidence
capture, which joins each registered app to the program that supervises it. Each
of them reaches the drop-ins by hand, because neither ``configparser`` nor a
plain ``cat`` follows supervisord's ``[include]`` -- that is a supervisord
feature, not a configparser one -- and each expands the glob the config
declares rather than assuming a directory name.

That makes the glob a real contract, and one that fails *open*: a reader that
misses the drop-ins still parses a valid config, just an empty one, and its
assertions pass over nothing at all. These tests pin the contract so that
degradation is loud, and ``test_every_reader_expands_the_include_globs_alike``
pins the readers against each other, since each carries its own copy of the
expansion and a copy that drifts fails in exactly that silent way.
"""

from __future__ import annotations

import ast
import configparser
import glob
import importlib.util
import re
import shlex
import subprocess
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

# The one cross-repo reader of this config, vendored here as part of every release.
_VENDORED_EVALS_CAPTURE = (
    _REPO_ROOT / "system/vendor/mngr/apps/minds_evals/imbue/minds_evals/evidence_collection.py"
)


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
# sit in four separate uv workspace members, and two of them are standalone scripts (the scaffolder
# declares its own PEP 723 dependencies by design). So each carries its own copy of the expansion,
# and this file pins the copies against each other instead.
_READER_PATHS: dict[str, Path] = {
    "scaffolder": _REPO_ROOT / ".agents/skills/build-app/scripts/scaffold_flask_lib.py",
    "migrate_workspace": _REPO_ROOT / ".agents/skills/migrate-workspace/scripts/migrate_workspace.py",
    "app_manifests": _REPO_ROOT / "system/test_app_manifests.py",
    "oom_bands": _REPO_ROOT / "system/services/oom_priority/bin/oom_tag_service_test.py",
}


def _load(name: str) -> ModuleType:
    """Import a reader by path, under its own module name so pytest's own copy is untouched."""
    spec = importlib.util.spec_from_file_location("_supervisord_reader_" + name, _READER_PATHS[name])
    assert spec is not None and spec.loader is not None, f"cannot load {_READER_PATHS[name]}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _declared_in(paths: list[Path]) -> set[str]:
    return {program for path in paths for program in _SECTION_RE.findall(path.read_text())}


def _program_names_in(paths: list[Path]) -> set[str]:
    return {name for path in paths for name in _PROGRAM_ONLY_RE.findall(path.read_text())}


def _write_workspace_with_dropins_at(root: Path, dropin_dir: str) -> None:
    """A workspace whose programs live in a directory of its own choosing, declared by glob."""
    (root / "system" / dropin_dir).mkdir(parents=True)
    (root / "system/supervisord.conf").write_text(
        "[supervisord]\nnodaemon=true\n\n[include]\nfiles = %(here)s/" + dropin_dir + "/*.conf\n"
    )
    (root / "system" / dropin_dir / "todo.conf").write_text("[program:todo]\ncommand=todo-app\n")
    (root / "system" / dropin_dir / "watchdog.conf").write_text(
        "[eventlistener:watchdog]\ncommand=watchdog\n"
    )


def test_every_reader_expands_the_include_globs_alike() -> None:
    """All four in-repo readers see the same programs this file does, for the shipped config.

    The expansion is written out once per reader, so any two can drift apart -- and the failure is
    silent in the same way a missed glob is: the reader that drifted returns a smaller set, or an
    empty one, and goes on asserting over it. Nothing else compares them; the other tests here each
    check the layout against itself.

    A reader is compared on what it concludes, not on how it spells the expansion, so a rewrite
    that keeps the answer is free.
    """
    canonical_files = [_SUPERVISORD_CONF] + _expand_include_patterns(_parse_main_config())
    canonical = _declared_in(canonical_files)
    assert canonical, "no programs found at all -- the fixture, not the readers, is wrong"

    assert _declared_in(_load("scaffolder")._supervisord_conf_files(_SUPERVISORD_CONF)) == canonical
    assert _declared_in(_load("migrate_workspace")._local_supervisord_configs(_REPO_ROOT)) == canonical
    # These two return command-by-name rather than a file list; the names are the shared claim.
    # The band reader covers event listeners as well, the manifest reader programs only.
    assert set(_load("oom_bands")._command_by_supervisord_program()) == canonical
    assert set(_load("app_manifests")._command_by_program()) == _program_names_in(canonical_files)


def test_every_reader_honours_a_dropin_directory_the_config_names_itself(tmp_path: Path) -> None:
    """Which directory holds the drop-ins is the workspace's to declare, not a constant to assume.

    Agreeing about the shipped config is not enough to prove that: this repo declares
    ``supervisord.conf.d/*.conf``, so a reader that simply hardcodes that directory agrees with
    every other reader and the test above stays green. It is only against a workspace that chose a
    different name that assuming and reading come apart -- and the reader that assumes finds nothing
    at all, which is the silent-empty-set failure this file exists to make loud.

    ``migrate-workspace`` in particular reads a FOREIGN workspace over SSH, one someone else
    configured, so it is the reader for which this matters most.
    """
    _write_workspace_with_dropins_at(tmp_path, "programs.d")
    conf = tmp_path / "system/supervisord.conf"
    expected = {"todo", "watchdog"}

    scaffolder = _load("scaffolder")
    migrate = _load("migrate_workspace")

    assert _declared_in(scaffolder._supervisord_conf_files(conf)) == expected
    assert _declared_in(migrate._local_supervisord_configs(tmp_path)) == expected

    # The other two take their config path from a module constant rather than an argument, so they
    # are pointed at this workspace by rebinding it on the private copy loaded above -- these are
    # throwaway module objects, not the ones pytest collected.
    oom_bands = _load("oom_bands")
    oom_bands._SUPERVISORD_CONF = conf
    app_manifests = _load("app_manifests")
    app_manifests._SUPERVISORD_CONF = conf

    assert set(oom_bands._command_by_supervisord_program()) == expected
    assert set(app_manifests._command_by_program()) == {"todo"}


# The two names the gate lifts out of the vendored capture. Named here because a rename upstream
# must fail loudly with an instruction, rather than quietly retiring the gate.
_CAPTURE_BUILDER = "supervisord_config_capture_command"
_CAPTURE_CONF_CONSTANT = "SUPERVISORD_CONF_RELATIVE_PATH"


def _vendored_capture_command(repo_root: Path) -> str | None:
    """The shell the vendored evals capture would run against ``repo_root``, or None if it has none.

    Lifted out of the source rather than imported: the module it lives in pulls in the whole
    minds_evals dependency tree (harbor, modal, pydantic), none of which this repo installs. Only
    the builder and the one constant it formats in are taken, with decorators stripped -- the
    ``@pure`` marker comes from a package that is not here either.
    """
    sources: dict[str, str] = {}
    for node in ast.parse(_VENDORED_EVALS_CAPTURE.read_text()).body:
        if isinstance(node, ast.FunctionDef) and node.name == _CAPTURE_BUILDER:
            node.decorator_list = []
            sources[node.name] = ast.unparse(node)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == _CAPTURE_CONF_CONSTANT and node.value is not None:
                sources[node.target.id] = "{} = {}".format(node.target.id, ast.unparse(node.value))
    if sources.keys() != {_CAPTURE_BUILDER, _CAPTURE_CONF_CONSTANT}:
        return None
    namespace: dict[str, object] = {}
    exec("\n".join(sources[name] for name in (_CAPTURE_CONF_CONSTANT, _CAPTURE_BUILDER)), namespace)
    builder = namespace[_CAPTURE_BUILDER]
    assert callable(builder)
    return builder(shlex.quote(str(repo_root)))


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


def test_a_program_in_a_dropin_implies_an_include_aware_vendored_capture() -> None:
    """The cross-repo release gate, as a check this repo's CI can actually see.

    The minds evals evidence capture reads a workspace's ``system/supervisord.conf`` from outside
    the container and joins every registered app to the ``[program:*]`` block whose
    ``forward_port.py`` call registers it. A capture that stops at that one file sees only the
    programs the main config still declares, and reports every app declared in a drop-in as having
    nothing supervising it -- silently, since an app-free workspace gives the same empty answer.

    Provisioning is tag-pinned and ``update-self`` is ceilinged to the running app's template ref,
    so landing this on ``main`` harms nobody. What binds is the release cut: a ``minds-v<N>``
    template tag must not declare programs in drop-ins unless the mngr commit tagged ``minds-v<N>``
    carries the include-aware capture. ``system/vendor/mngr`` is synced as part of that same
    release, so the vendored copy is the artifact this repo can check.

    Deliberately a conditional: it says nothing about a template that declares every program in the
    main config, and is a permanent regression guard for one that does not.

    Asserts on what the capture *does*, not on how its shell is written: it builds the capture's
    own shell out of the vendored source and runs it against this repo, so a rewrite of the
    expansion -- a different ``sed``, an ``awk``, a pipeline -- keeps this green where matching on
    the source text would fail a correct implementation. What it does still depend on is the shape
    of the upstream API: a builder of that name returning a shell string, and the one constant it
    formats in. Rename either, or read the config in Python instead of emitting shell, and the
    gate goes red -- deliberately, since it cannot tell that apart from the capture being dropped.
    The assertions below carry the re-point instruction for that case.
    """
    if not list(_DROPIN_DIR.glob("*.conf")):
        return

    # A missing capture -- or a missing vendored subtree around it -- is a failure, not a pass: the
    # path lives in another repo's tree, so an upstream rename would otherwise retire this gate
    # silently, leaving the drop-ins unguarded, which is the one outcome it exists to prevent.
    assert _VENDORED_EVALS_CAPTURE.is_file(), (
        f"{_VENDORED_EVALS_CAPTURE.relative_to(_REPO_ROOT)} is not in the vendored mngr subtree, "
        "so the release gate below cannot read the evals evidence capture. If it moved upstream, "
        "re-point this test at its new path -- do not drop the check."
    )

    command = _vendored_capture_command(_REPO_ROOT)
    assert command is not None, (
        f"{_VENDORED_EVALS_CAPTURE.relative_to(_REPO_ROOT)} defines no {_CAPTURE_BUILDER!r}, so it "
        "reads the main config alone and would find no program for anything this template declares "
        f"in {_DROPIN_DIR.relative_to(_REPO_ROOT)}.\n\n"
        "This is the release gate, not a broken test: land the include-aware capture in mngr, then "
        "re-sync system/vendor/mngr. If the builder was instead RENAMED upstream, re-point this "
        "test at the new name -- do not drop the check."
    )
    captured = subprocess.run(
        ["bash", "-c", command], capture_output=True, text=True, cwd="/", timeout=60
    ).stdout
    # Run from `/`, which is neither the repo nor the config's directory: a capture that resolves
    # its globs against its own working directory would pass from the repo root by luck.
    reached = set(_SECTION_RE.findall(captured))
    declared = _declared_in([_SUPERVISORD_CONF] + _expand_include_patterns(_parse_main_config()))
    assert reached >= declared, (
        f"{_VENDORED_EVALS_CAPTURE.relative_to(_REPO_ROOT)} does not follow supervisord's "
        "[include] globs: run against this repo it reaches "
        f"{sorted(reached) or 'no program at all'}, missing {sorted(declared - reached)}, which "
        f"this template declares in {_DROPIN_DIR.relative_to(_REPO_ROOT)}. The evals capture would "
        "find no program for those apps and misgrade every one of them.\n\n"
        "This is the release gate, not a broken test. To satisfy it: land the include-aware "
        "capture in mngr, then re-sync system/vendor/mngr. Do NOT relax this assertion -- the "
        "alternative is shipping a template tag that misgrades every eval run on the matching "
        "minds release."
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
