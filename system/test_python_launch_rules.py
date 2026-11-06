"""How Python gets launched in this workspace (see .agents/shared/references/running-python.md).

Each rule is checked against the real tree and against a planted violation:

- A long-running program never runs under ``uv run``, so no ``uv`` process stays
  resident as its parent: this covers supervisord programs (built-in and user-added
  drop-ins alike) and the tmux windows ``.mngr/settings.toml`` opens. One that starts
  a root-venv entry point runs ``exec .venv/bin/<name>`` (a service after a ``--frozen``
  sync).
- A one-off written into an agent-facing file, or into a string or argv in the
  workspace's Python, runs ``uv run --no-sync``, so following an instruction can
  never relock or sync the workspace behind the agent's back. A one-off of a user
  package's command names that package's project (``--project``), since the command
  is installed only in the package's own venv; a user package's own program line
  syncs that project with ``--locked`` and execs from its ``.venv/bin``, which
  ``system/test_user_packages_are_standalone.py`` checks.
- A ``python3 <path>.py`` written into an agent-facing file names a bare entry point: the
  system ``python3`` has no venv, so a venv-tier script run that way fails at its first
  third-party import. A ``uv run <path>.py`` names a script that exists, so a path that
  moved is rewritten wherever it is written down.
- Code imports packages. An edit to the import path (``sys.path``, ``PYTHONPATH``, pytest's
  ``pythonpath``), and production code naming one of the workspace's own scripts, modules or
  commands to run it as a process, each fail unless a marker names why it has to stay.
  Loading a module from a file path is counted, and the count only goes down.
"""

from __future__ import annotations

import ast
import configparser
import re
import shlex
import subprocess
import tomllib
from collections.abc import Collection, Sequence
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from agents_shared.validate_skill import is_entry_module, third_party_skills
from entry_points_testing import (
    BARE_SEARCH_PATHS,
    REPO_ROOT,
    is_bare_entry,
    is_test_file,
)
from imbue.mngr.interfaces.host import NamedCommand
from inline_snapshot import snapshot
from vendor_testing import resolves_into_vendor
from workspace_bare_scripts import user_packages

_SUPERVISORD_DIR = REPO_ROOT / "system"
_MNGR_SETTINGS = REPO_ROOT / ".mngr" / "settings.toml"

_SHELL_PROGRAMS = frozenset({"bash", "sh", "zsh", "dash"})
_EXTRA_WINDOW_KEYS = frozenset(
    {"extra_window", "extra_window__extend", "extra_window__assign"}
)

# The file types agents read commands out of (docs, prompts, config, scripts).
_AGENT_FACING_SUFFIXES = (".md", ".toml", ".sh", ".conf")
# The uv run rule also reads the strings and argv in the workspace's Python.
_UV_RUN_SCANNED_SUFFIXES = (*_AGENT_FACING_SUFFIXES, ".py")
# The python3 rule also reads the hook configs.
_PYTHON3_SCANNED_SUFFIXES = (*_AGENT_FACING_SUFFIXES, ".json")
# Path parts whose files are records, third-party code or test inputs, not instructions.
_EXEMPT_PARTS = frozenset({"changelog", "vendor", "fixtures"})
_EXEMPT_NAMES = frozenset({"CHANGELOG.md", "UNABRIDGED_CHANGELOG.md"})
# Dated design records under docs/, which quote the commands of their day. The living
# contracts among them are what agents are pointed at as the rules, so those are scanned.
_DOCS_ROOT = "docs"
_DESIGN_RECORD_PARTS = frozenset({"blueprint", "specs"})
_LIVING_CONTRACT_NAME = "contracts.md"

# uv's global options that take a separate value word; any of them, or a flag, may come
# between ``uv`` and its ``run`` subcommand.
_UV_GLOBAL_OPTIONS_WITH_VALUES = (
    "--directory",
    "--project",
    "--config-file",
    "--cache-dir",
    "--color",
)
_UV_RUN_RE = re.compile(
    r"\buv(?:\s+(?:(?:"
    + "|".join(_UV_GLOBAL_OPTIONS_WITH_VALUES)
    + r")(?:=|\s+)\S+|--?[a-z][\w-]*))*\s+run\b"
)
# Options after which ``uv run`` neither syncs the workspace venv nor touches uv.lock.
_NON_SYNCING_OPTIONS = frozenset({"--no-sync", "--no-project"})
# uv run options that take a separate value word.
_OPTIONS_WITH_VALUES = frozenset(
    {
        "--package",
        "--with",
        "--env-file",
        "--python",
        "--directory",
        "--project",
        "--group",
    }
)

# Deliberate plain ``uv run`` lines: (repo-relative path, a substring of the line).
_DELIBERATE_SYNCS: tuple[tuple[str, str], ...] = (
    # The update worker reads footprints and test selections with the app-manifest
    # library as the merge left it, so it syncs that package from the merged lock first.
    (
        ".agents/skills/update-self/references/update-self-worker.md",
        "uv run --frozen --package app-manifest app-manifest",
    ),
    # The migration reference quotes the forms an existing workspace's files still use.
    (
        ".agents/skills/update-self/references/python-packaging-migration.md",
        "runs `uv run <name>`",
    ),
    (
        ".agents/skills/update-self/references/python-packaging-migration.md",
        "a plain `uv run X` becomes",
    ),
    # Starts Imbue Studio from a source checkout of the mngr repo on the user's machine, which
    # keeps no synced venv of its own; nothing about the workspace's venv applies there.
    (
        ".agents/skills/imbue-studio/SKILL.md",
        "uv run minds run --no-browser",
    ),
)


def _shell_command_string(words: list[str], shell_index: int) -> str | None:
    """The command string a shell at ``shell_index`` runs (``-c``, ``-lc``, ``--login -c``), or None.

    ``-o`` / ``-O`` take the next word as their value (``-o pipefail``, ``-euo pipefail``), so
    the string is the first word after the options.
    """
    takes_command = False
    arguments = iter(words[shell_index + 1 :])
    for word in arguments:
        if not word.startswith(("-", "+")):
            return word if takes_command else None
        if word.startswith("--"):
            continue
        letters = word[1:]
        if "c" in letters:
            takes_command = True
        for _ in range(letters.count("o") + letters.count("O")):
            next(arguments, None)
    return None


def _past_options(
    words: Sequence[object], index: int, options_with_values: Collection[str]
) -> int:
    """The index of the first word from ``index`` on that is not an option or an option's value."""
    while (
        index < len(words)
        and isinstance(words[index], str)
        and str(words[index]).startswith("-")
    ):
        index += 2 if words[index] in options_with_values else 1
    return index


def _uv_subcommand(arguments: list[str]) -> str | None:
    """The subcommand ``uv`` runs from ``arguments``, past its global options."""
    index = _past_options(arguments, 0, _UV_GLOBAL_OPTIONS_WITH_VALUES)
    return arguments[index] if index < len(arguments) else None


def _runs_uv_run(command: str) -> bool:
    """Whether a program command executes ``uv run``, directly or inside a ``bash -c`` string."""
    words = shlex.split(command)
    for index, word in enumerate(words):
        if (
            PurePosixPath(word).name == "uv"
            and _uv_subcommand(words[index + 1 :]) == "run"
        ):
            return True
        if PurePosixPath(word).name in _SHELL_PROGRAMS:
            shell_command = _shell_command_string(words, index)
            if shell_command is not None and _runs_uv_run(shell_command):
                return True
    return False


def _program_commands(supervisord_dir: Path) -> dict[str, str]:
    """Every supervisord program's and event listener's command, keyed by its section.

    ``configparser`` reads supervisord's ini format the way supervisord does: it folds
    continuation lines, skips comments, and accepts ``command = ...`` with spaces.
    It does not follow ``[include]``, so the drop-ins are read after the main config.
    Interpolation is off because supervisord's ``%(ENV_x)s`` is not configparser's.
    """
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(supervisord_dir / "supervisord.conf")
    parser.read(sorted((supervisord_dir / "supervisord.conf.d").glob("*.conf")))
    return {
        section: parser[section]["command"]
        for section in parser.sections()
        if section.startswith(("program:", "eventlistener:"))
        and "command" in parser[section]
    }


def _programs_under_uv_run(supervisord_dir: Path) -> list[str]:
    return [
        f"[{section}]: {command}"
        for section, command in _program_commands(supervisord_dir).items()
        if _runs_uv_run(command)
    ]


def _extra_window_commands(settings: object) -> list[str]:
    """Every command an ``extra_window`` setting opens, at any depth of mngr's settings."""
    if isinstance(settings, list):
        return [
            command for item in settings for command in _extra_window_commands(item)
        ]
    if not isinstance(settings, dict):
        return []
    commands: list[str] = []
    for key, value in settings.items():
        if key in _EXTRA_WINDOW_KEYS:
            commands.extend(NamedCommand.from_string(entry).command for entry in value)
        else:
            commands.extend(_extra_window_commands(value))
    return commands


def _windows_under_uv_run(settings_toml: Path) -> list[str]:
    settings = tomllib.loads(settings_toml.read_text())
    return [
        command for command in _extra_window_commands(settings) if _runs_uv_run(command)
    ]


def _is_compliant_uv_run(rest: str) -> bool:
    """Whether the text after one ``uv run`` names a non-syncing run (or only mentions the command).

    ``rest`` starts right after ``uv run``. Anything but a space there (a closing
    backtick or quote, punctuation, the end of the line) is prose naming the command,
    not an invocation.
    """
    if not rest.startswith(" "):
        return True
    words = [word.strip("`'\"()*,.;:") for word in rest.split()]
    index = 0
    while index < len(words) and words[index].startswith("--"):
        if words[index] in _NON_SYNCING_OPTIONS:
            return True
        index += 2 if words[index] in _OPTIONS_WITH_VALUES else 1
    return False


def _plain_uv_runs(text: str) -> list[tuple[int, str]]:
    """Every ``(line number, line)`` in ``text`` with a plain, syncing ``uv run``."""
    return [
        (number, line)
        for number, line in enumerate(text.splitlines(), start=1)
        for match in _UV_RUN_RE.finditer(line)
        if not _is_compliant_uv_run(line[match.end() :])
    ]


def _argv_runs_plain_uv_run(elements: list[ast.expr]) -> bool:
    """Whether a list or tuple literal is a ``uv run`` argv that syncs.

    Only the leading string literals are read: an argv that names no non-syncing
    option before its first computed element (the program, say) syncs. A bare
    ``("uv", "run")`` names no program, so it is not judged: it is a comparison
    against a command's first words or a prefix that another argv spreads.
    """
    literals: list[str] = []
    for element in elements:
        if not (isinstance(element, ast.Constant) and isinstance(element.value, str)):
            break
        literals.append(element.value)
    if literals[:2] != ["uv", "run"] or len(elements) < 3:
        return False
    return not _is_compliant_uv_run(" " + " ".join(literals[2:]))


def _python_plain_uv_runs(source: str) -> list[tuple[int, str]]:
    """Every ``(line number, text)`` in Python source whose string or argv literal holds a plain ``uv run``."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.extend((node.lineno, line) for _, line in _plain_uv_runs(node.value))
        elif isinstance(node, (ast.List, ast.Tuple)) and _argv_runs_plain_uv_run(
            node.elts
        ):
            found.append((node.lineno, ast.unparse(node)))
    return found


def _is_test_source(path: PurePosixPath) -> bool:
    return (
        path.name.endswith("_test.py")
        or path.name.startswith("test_")
        or path.name in {"conftest.py", "testing.py"}
    )


def _is_scanned(relative: str, suffixes: tuple[str, ...]) -> bool:
    path = PurePosixPath(relative)
    if _EXEMPT_PARTS.intersection(path.parts) or path.name in _EXEMPT_NAMES:
        return False
    if (
        path.parts[0] == _DOCS_ROOT
        and _DESIGN_RECORD_PARTS.intersection(path.parts)
        and path.name != _LIVING_CONTRACT_NAME
    ):
        return False
    if path.suffix == ".py" and _is_test_source(path):
        return False
    return relative.endswith(suffixes)


def _is_in_third_party_skill(relative: str, repo_root: Path) -> bool:
    """Whether a file belongs to a skill listed as a third party's, kept as it came: not ours to police."""
    parts = PurePosixPath(relative).parts
    return (
        parts[:2] == (".agents", "skills")
        and len(parts) > 3
        and parts[2] in third_party_skills(repo_root / ".agents")
    )


def _scanned_files(
    suffixes: tuple[str, ...] = _UV_RUN_SCANNED_SUFFIXES, repo_root: Path = REPO_ROOT
) -> list[Path]:
    """The tracked files agents read commands from.

    A link into ``system/vendor/`` is skipped like the vendored tree itself: once the mngr
    assets are fetched, ``docs/system/style_guide.md`` resolves to mngr's style guide, whose
    commands are written for the mngr repo.
    """
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    return [
        repo_root / relative
        for relative in tracked
        if relative
        and _is_scanned(relative, suffixes)
        and not _is_in_third_party_skill(relative, repo_root)
        and (repo_root / relative).is_file()
        and not resolves_into_vendor(repo_root / relative, repo_root)
    ]


def _plain_uv_run_violations() -> list[tuple[str, str]]:
    violations: list[tuple[str, str]] = []
    for path in _scanned_files():
        text = path.read_text(errors="replace")
        found = (
            _python_plain_uv_runs(text)
            if path.suffix == ".py"
            else _plain_uv_runs(text)
        )
        violations.extend((str(path.relative_to(REPO_ROOT)), line) for _, line in found)
    return violations


def _is_deliberate(relative: str, line: str) -> bool:
    return any(
        relative == path and context in line for path, context in _DELIBERATE_SYNCS
    )


def test_no_supervisord_program_runs_under_uv_run() -> None:
    offenders = _programs_under_uv_run(_SUPERVISORD_DIR)
    assert not offenders, (
        "These supervisord programs run under `uv run`, which stays resident as their parent and can "
        'relock the workspace. A built-in uses `bash -c "uv sync --all-packages --frozen && exec '
        '.venv/bin/<name>"`; a user package syncs and execs its own project instead ("User packages '
        'are standalone projects" in .agents/shared/references/running-python.md):\n'
        + "\n".join(offenders)
    )


def test_a_planted_uv_run_program_is_caught_in_any_form(tmp_path: Path) -> None:
    dropins = tmp_path / "supervisord.conf.d"
    dropins.mkdir()
    (tmp_path / "supervisord.conf").write_text("[supervisord]\nnodaemon=true\n")
    (dropins / "direct.conf").write_text("[program:direct]\ncommand=uv run direct\n")
    (dropins / "spaced.conf").write_text(
        '[program:spaced]\ncommand = bash -c\n    "exec uv run spaced"\n'
    )
    (dropins / "tagged.conf").write_text(
        "[program:tagged]\ncommand=python3 system/services/oom_priority/bin/oom_tag_service.py "
        'user bash -c "python3 system/scripts/forward_port.py --name t --url http://x && exec uv run tagged"\n'
    )
    (dropins / "login.conf").write_text(
        '[program:login]\ncommand=bash -lc "exec uv run login"\n'
    )
    (dropins / "longopt.conf").write_text(
        '[program:longopt]\ncommand=bash --login -c "exec uv run longopt"\n'
    )
    (dropins / "pipefail.conf").write_text(
        '[program:pipefail]\ncommand=bash -euo pipefail -c "exec uv run pipefail"\n'
    )
    (dropins / "quiet.conf").write_text(
        '[program:quiet]\ncommand=bash -c "uv --directory /srv -q run quiet"\n'
    )
    (dropins / "fine.conf").write_text(
        "; never `command=uv run fine`: uv would stay resident as its parent\n"
        '[program:fine]\ncommand=bash -c "uv sync --all-packages --frozen && exec .venv/bin/fine"\n'
    )

    offenders = _programs_under_uv_run(tmp_path)

    assert [offender.split("]", 1)[0] for offender in offenders] == [
        "[program:direct",
        "[program:login",
        "[program:longopt",
        "[program:pipefail",
        "[program:quiet",
        "[program:spaced",
        "[program:tagged",
    ]


def test_no_mngr_window_runs_under_uv_run() -> None:
    offenders = _windows_under_uv_run(_MNGR_SETTINGS)
    assert not offenders, (
        "These tmux windows run under `uv run`, which stays resident as the parent of everything "
        "they start. Sync `--frozen`, then `exec .venv/bin/<name>`:\n"
        + "\n".join(offenders)
    )


def test_a_planted_uv_run_window_is_caught_at_any_depth(tmp_path: Path) -> None:
    settings = tmp_path / "settings.toml"
    settings.write_text(
        "[create_templates.main]\n"
        'extra_window = ["boot=\'bash -c \\"uv sync --frozen; exec uv run --no-sync boot\\"\'"]\n'
        "[create_templates.worker]\n"
        'extra_window__extend = ["setup=uv run setup", "fine=\'bash -c \\"exec .venv/bin/fine\\"\'"]\n'
        "[create_templates.services]\n"
        'extra_window__assign = ["svc=uv run svc"]\n'
    )

    assert _windows_under_uv_run(settings) == [
        'bash -c "uv sync --frozen; exec uv run --no-sync boot"',
        "uv run setup",
        "uv run svc",
    ]


def test_no_plain_uv_run_in_agent_facing_files() -> None:
    offenders = [
        f"{relative}: {line.strip()}"
        for relative, line in _plain_uv_run_violations()
        if not _is_deliberate(relative, line)
    ]
    assert not offenders, (
        "A plain `uv run` checks the lock and syncs the venv, and can rewrite uv.lock mid-task. "
        "Write one-offs as `uv run --no-sync ...` (see .agents/shared/references/running-python.md):\n"
        + "\n".join(offenders)
    )


def test_every_deliberate_sync_exception_still_matches_a_line() -> None:
    violations = _plain_uv_run_violations()
    stale = [
        (path, context)
        for path, context in _DELIBERATE_SYNCS
        if not any(
            relative == path and context in line for relative, line in violations
        )
    ]
    assert not stale, (
        f"These deliberate-sync exceptions no longer match any line: {stale}"
    )


def test_the_plain_uv_run_scan_catches_a_planted_command_and_skips_mentions() -> None:
    text = "\n".join(
        [
            "Run `uv run app-manifest validate-manifest x` first.",
            "    uv run pytest system/apps/foo",
            "Then `uv run --no-sync tk start x`; never wrap a service in `uv run`.",
            "uv run --no-project --with 'pydantic>=2' python check.py",
            "uv run --frozen --package app-manifest app-manifest footprint",
            "uv run system/scripts/workspace_scripts/verify_app.py todo",
            "uv run python3 .agents/skills/notify-user/python/notify_user_skill/notify_user.py done",
            "uv run --no-sync system/scripts/workspace_scripts/verify_app.py todo",
            "uv --quiet run pytest; uv --directory /srv run --no-sync tk ls",
            "Check `uv --version` to run the right one.",
        ]
    )

    assert [number for number, _ in _plain_uv_runs(text)] == [1, 2, 5, 6, 7, 9]


def test_the_python_scan_catches_strings_and_argv_and_skips_comparisons() -> None:
    source = "\n".join(
        [
            'HINT = "Run `uv run host-backup-heavy-dirs` to see them."',
            'subprocess.run(["uv", "run", "env-converge", "upgrade"])',
            'subprocess.run(["uv", "run", "--no-sync", "env-converge", "upgrade"])',
            'is_uv_run = words[:2] == ("uv", "run")',
            'subprocess.run(["uv", "run", script, "--help"])',
            "# uv run app-manifest in a comment is not a string",
        ]
    )

    assert [number for number, _ in _python_plain_uv_runs(source)] == [1, 2, 5]


def test_scanning_skips_records_and_tests_but_reaches_living_contracts() -> None:
    scanned = {str(path.relative_to(REPO_ROOT)) for path in _scanned_files()}

    assert not any(
        "/changelog/" in path or path.endswith("CHANGELOG.md") for path in scanned
    )
    assert (
        "system/apps/chat/imbue/chat/harnesses/antigravity/fixtures/README.md"
        not in scanned
    )
    assert not any("fixtures" in PurePosixPath(path).parts for path in scanned)
    assert not any(PurePosixPath(path).name.endswith("_test.py") for path in scanned)
    assert (
        "docs/system/blueprint/workspace-app-model/plan-workspace-app-model.md"
        not in scanned
    )
    assert "docs/system/blueprint/workspace-app-model/contracts.md" in scanned
    # A skill named like a design-record directory is still agent-facing.
    assert ".agents/skills/blueprint/SKILL.md" in scanned
    assert "system/services/host_backup/src/host_backup/runner.py" in scanned
    assert "AGENTS.md" in scanned


def test_a_link_into_vendored_code_is_not_scanned(tmp_path: Path) -> None:
    (tmp_path / "system" / "vendor" / "assets").mkdir(parents=True)
    (tmp_path / "system" / "vendor" / "assets" / "guide.md").write_text(
        "uv run python scripts/build.py\n"
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").symlink_to("../system/vendor/assets/guide.md")
    (tmp_path / "docs" / "own.md").write_text("uv run --no-sync tk ls\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)

    scanned = {
        str(path.relative_to(tmp_path)) for path in _scanned_files(repo_root=tmp_path)
    }

    assert scanned == {"docs/own.md"}


# ``python3 [flags] <path>.py``, the path optionally quoted (JSON escapes its quotes).
_PYTHON3_SCRIPT_RE = re.compile(
    r"(?<![\w./-])python3((?:\s+-[A-Za-z]+)*)\s+\\?[\"']?([^\s\"'`)\\]+\.py)\b"
)
# A ``python3`` that ``uv run`` starts runs from the venv, so it is not this rule's concern.
_UNDER_UV_RUN_RE = re.compile(r"\buv run(?:\s+--[\w-]+)*\s+$")
# Spellings of the repo root (and of update-self's staged copy of it) in commands.
_REPO_ROOT_PREFIXES = (
    "${MNGR_AGENT_WORK_DIR:-.}/",
    "${MNGR_AGENT_WORK_DIR}/",
    "$MNGR_AGENT_WORK_DIR/",
    "${REPO_ROOT}/",
    "$REPO_ROOT/",
    "/home/user/workspace/",
    "data/.tasks/update-self/skill-at-target/",
)


def _resolve_written_path(written: str, file_dir: Path, repo_root: Path) -> Path | None:
    """Where a script path written in a command points; None when a variable leaves it unknown."""
    path = written
    base = repo_root
    for prefix in _REPO_ROOT_PREFIXES:
        if path.startswith(prefix):
            path = path.removeprefix(prefix)
            break
    else:
        if path.startswith("$script_dir/"):
            path, base = path.removeprefix("$script_dir/"), file_dir
    return None if "$" in path else (base / path).resolve()


def _names_no_tree_file(written: str) -> bool:
    """A placeholder (``<name>``) or a scratch path under /tmp names no file in the tree."""
    return "<" in written or written.startswith("/tmp/")


def _named_python3_scripts(
    text: str, file_dir: Path, repo_root: Path
) -> list[tuple[str, Path | None]]:
    """Every ``(as written, resolved path)`` a ``python3`` in ``text`` runs; None when it cannot be resolved."""
    found: list[tuple[str, Path | None]] = []
    for line in text.splitlines():
        for match in _PYTHON3_SCRIPT_RE.finditer(line):
            if _UNDER_UV_RUN_RE.search(line[: match.start()]):
                continue
            written = match.group(2)
            if _names_no_tree_file(written):
                continue
            found.append((written, _resolve_written_path(written, file_dir, repo_root)))
    return found


# ``uv run [options] <path>.py``, the path optionally quoted (JSON escapes its quotes).
_UV_RUN_SCRIPT_RE = re.compile(
    r"\buv run((?:\s+--[\w-]+)*)\s+\\?[\"']?([^\s\"'`)\\]+\.py)\b"
)


def _missing_uv_run_scripts(
    files: list[Path], repo_root: Path
) -> list[tuple[Path, str]]:
    """Every ``(file, path as written)`` for a ``uv run <path>.py`` whose script is not in the tree."""
    missing: list[tuple[Path, str]] = []
    for path in files:
        for line in path.read_text(errors="replace").splitlines():
            for match in _UV_RUN_SCRIPT_RE.finditer(line):
                written = match.group(2)
                if _names_no_tree_file(written):
                    continue
                resolved = _resolve_written_path(written, path.parent, repo_root)
                if resolved is not None and not resolved.is_file():
                    missing.append((path, written))
    return missing


def _python3_violations(files: list[Path], repo_root: Path) -> list[str]:
    return [
        f"{path.relative_to(repo_root)}: python3 {written}"
        for path in files
        for written, resolved in _named_python3_scripts(
            path.read_text(errors="replace"), path.parent, repo_root
        )
        if resolved is None or not is_bare_entry(resolved)
    ]


def test_python3_invocations_name_bare_entry_points() -> None:
    offenders = _python3_violations(
        _scanned_files(_PYTHON3_SCANNED_SUFFIXES), REPO_ROOT
    )
    assert not offenders, (
        "These run a script with the system python3, but it is not a bare entry point (or does not exist): "
        "the system python3 has no venv. Run it with `uv run --no-sync <path>` instead "
        "(see .agents/shared/references/running-python.md):\n" + "\n".join(offenders)
    )


def test_the_python3_scan_catches_a_venv_script_and_a_stale_path(
    tmp_path: Path,
) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text(
        "\n".join(
            [
                "python3 .agents/shared/scripts/agents_shared/validate_skill.py skill-dir",
                "python3 system/scripts/forward_port.py --name x --url http://localhost:1",
                "python3 system/scripts/no_such_script.py",
                "uv run --no-sync python3 .agents/shared/scripts/agents_shared/validate_skill.py skill-dir",
                'command": "python3 \\"$MNGR_AGENT_WORK_DIR/system/scripts/agent_rewrite_bash_command.py\\""',
            ]
        )
    )

    offenders = [
        written
        for written, resolved in _named_python3_scripts(
            doc.read_text(), doc.parent, REPO_ROOT
        )
        if resolved is None or not is_bare_entry(resolved)
    ]

    assert offenders == [
        ".agents/shared/scripts/agents_shared/validate_skill.py",
        "system/scripts/no_such_script.py",
    ]


def test_uv_run_invocations_name_scripts_that_exist() -> None:
    offenders = [
        f"{path.relative_to(REPO_ROOT)}: uv run {written}"
        for path, written in _missing_uv_run_scripts(
            _scanned_files(_PYTHON3_SCANNED_SUFFIXES), REPO_ROOT
        )
    ]
    assert not offenders, (
        "These run a script that is not in the tree, likely one that moved into its package "
        "(see .agents/shared/references/running-python.md):\n" + "\n".join(offenders)
    )


def test_the_uv_run_scan_catches_a_moved_script(tmp_path: Path) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text(
        "\n".join(
            [
                "uv run --no-sync .agents/shared/scripts/validate_skill.py .agents/skills/x",
                "uv run --no-sync .agents/shared/scripts/agents_shared/validate_skill.py .agents/skills/x",
                'eval "$(uv run --no-sync .agents/shared/scripts/agents_shared/parse_task_frontmatter.py task.md)"',
                "uv run --no-sync .agents/skills/<name>/python/<name>_skill/run.py",
                "uv run --no-sync pytest system/no_such_test.py",
            ]
        )
    )

    assert _missing_uv_run_scripts([doc], REPO_ROOT) == [
        (doc, ".agents/shared/scripts/validate_skill.py")
    ]


# A launch of the workspace's own code or an import-path edit that has to stay says so in a
# marker among the comment lines directly above its statement,
# ``# own-process: <reason> -- <why>`` or ``# import-path: <reason> -- <why>``, naming one
# of the reasons running-python.md ("When a launch or an import-path edit has to stay")
# gives. Every unmarked site fails its test.
_OWN_PROCESS_MARKER = "own-process"
_IMPORT_PATH_MARKER = "import-path"
_REASONS_BY_MARKER: dict[str, frozenset[str]] = {
    _OWN_PROCESS_MARKER: frozenset(
        {"other-tier", "outlives-caller", "deadline", "other-tree", "wrapper", "server"}
    ),
    _IMPORT_PATH_MARKER: frozenset({"no-venv", "other-tree"}),
}
# Any comment that opens like a marker is one, so a malformed marker is reported rather than
# passed over.
_MARKER_RE = re.compile(r"^\s*#\s*(own-process|import-path):(.*)$")


class _Marker(NamedTuple):
    line: int
    kind: str
    reason: str
    why: str

    def is_valid(self) -> bool:
        return self.reason in _REASONS_BY_MARKER[self.kind] and bool(self.why)


class _Site(NamedTuple):
    """One launch or import-path edit, with the markers of its kind directly above its statement."""

    relative: str
    line: int
    text: str
    markers: tuple[_Marker, ...]

    def is_kept(self) -> bool:
        return any(marker.is_valid() for marker in self.markers)

    def describe(self) -> str:
        return f"{self.relative}:{self.line}: {self.text}"


def _markers(lines: list[str]) -> list[_Marker]:
    found: list[_Marker] = []
    for number, line in enumerate(lines, start=1):
        match = _MARKER_RE.match(line)
        if match:
            kind, rest = match.groups()
            reason, _, why = rest.partition("--")
            found.append(_Marker(number, kind, reason.strip(), why.strip()))
    return found


def _comment_block_above(lines: list[str], line: int) -> range:
    """The numbers of the unbroken run of comment lines directly above ``line``."""
    start = line
    while start > 1 and lines[start - 2].lstrip().startswith("#"):
        start -= 1
    return range(start, line)


def _statement_spans(tree: ast.AST) -> list[tuple[int, int]]:
    return [
        (node.lineno, node.end_lineno or node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.stmt)
    ]


def _statement_start(spans: list[tuple[int, int]], line: int) -> int:
    """The first line of the innermost statement holding ``line``."""
    return max((start for start, end in spans if start <= line <= end), default=line)


def _sites(
    relative: str,
    lines: list[str],
    flagged_lines: list[int],
    spans: list[tuple[int, int]],
    kind: str,
) -> list[_Site]:
    marker_by_line = {
        marker.line: marker for marker in _markers(lines) if marker.kind == kind
    }
    return [
        _Site(
            relative,
            line,
            lines[line - 1].strip(),
            tuple(
                marker_by_line[number]
                for number in _comment_block_above(lines, _statement_start(spans, line))
                if number in marker_by_line
            ),
        )
        for line in flagged_lines
    ]


# Calls that put a directory on the import path without naming ``sys.path``.
_IMPORT_PATH_CALLS = frozenset({"syspath_prepend", "addsitedir"})


def _is_pythonpath(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value == "PYTHONPATH"


def _sets_a_path(value: ast.expr) -> bool:
    """An empty ``PYTHONPATH`` clears the variable for a clean subprocess; anything else adds to it."""
    return not (isinstance(value, ast.Constant) and value.value == "")


def _sys_path_edit_lines(tree: ast.AST) -> list[int]:
    """The lines where code changes where imports are looked up: ``sys.path`` under any alias,
    the calls that prepend to it, or a ``PYTHONPATH`` written into an environment."""
    sys_names = {"sys"} | {
        alias.asname
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == "sys" and alias.asname
    }
    lines: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module == "sys"
            and any(a.name == "path" for a in node.names)
        ):
            lines.add(node.lineno)
        if isinstance(node, ast.Call):
            called = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", None)
            )
            if called in _IMPORT_PATH_CALLS or any(
                keyword.arg == "PYTHONPATH" and _sets_a_path(keyword.value)
                for keyword in node.keywords
            ):
                lines.add(node.lineno)
        if isinstance(node, ast.Dict) and any(
            _is_pythonpath(key) and _sets_a_path(value)
            for key, value in zip(node.keys, node.values)
        ):
            lines.add(node.lineno)
        if (
            isinstance(node, ast.Assign)
            and _sets_a_path(node.value)
            and any(
                isinstance(target, ast.Subscript) and _is_pythonpath(target.slice)
                for target in node.targets
            )
        ):
            lines.add(node.lineno)
        target = (
            node.func.value
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            else None
        )
        if isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            target = next(
                (t.value if isinstance(t, ast.Subscript) else t for t in targets), None
            )
        if (
            isinstance(target, ast.Attribute)
            and target.attr == "path"
            and isinstance(target.value, ast.Name)
            and target.value.id in sys_names
        ):
            lines.add(target.lineno)
    return sorted(lines)


# A file with none of these words and no ``<sys or its alias>.path`` is not parsed. The ordinary
# spelling of every edit ``_sys_path_edit_lines`` reads has one; an exotic one (a comment between
# ``sys`` and ``.path``, a ``PYTHONPATH`` key built from escapes) can pass, as ``getattr(sys,
# "path")`` passes the AST check itself: these scans catch ordinary code, not evasion.
_IMPORT_PATH_WORDS = ("PYTHONPATH", "syspath_prepend", "addsitedir", "from sys import")
_SYS_ALIAS_RE = re.compile(r"\bsys\s+as\s+(\w+)")


def _may_edit_import_path(text: str) -> bool:
    if any(word in text for word in _IMPORT_PATH_WORDS):
        return True
    names = {"sys", *_SYS_ALIAS_RE.findall(text)}
    return any(re.search(rf"\b{re.escape(name)}\s*\.\s*path\b", text) for name in names)


def _edits_sys_path(source: str) -> bool:
    return _may_edit_import_path(source) and bool(
        _sys_path_edit_lines(ast.parse(source))
    )


def _tracked_paths(repo_root: Path, pattern: str) -> list[str]:
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "--", pattern],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    return [
        relative
        for relative in tracked
        if relative
        and "vendor" not in PurePosixPath(relative).parts
        and not _is_in_third_party_skill(relative, repo_root)
        and (repo_root / relative).is_file()
    ]


def _tracked_python_files(repo_root: Path) -> list[str]:
    """The template's own tracked ``.py`` files, tests included.

    Vendored code and third-party skills are left out, and so is this file,
    whose planted cases would count.
    """
    return sorted(
        relative
        for relative in _tracked_paths(repo_root, "*.py")
        if relative != "system/test_python_launch_rules.py"
    )


_PYTEST_CONFIG_NAMES = frozenset(
    {"pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini"}
)
_PYTHONPATH_SETTING_RE = re.compile(r"^\s*pythonpath\s*=")


# A shell line that sets ``PYTHONPATH`` to something (an empty value clears it).
_SHELL_PYTHONPATH_RE = re.compile(
    r"(?<![\w$])PYTHONPATH=(?!(?:\"\"|'')?(?:\s|;|&|\||$))"
)


def _tracked_pytest_configs(repo_root: Path) -> list[str]:
    return sorted(
        relative
        for name in _PYTEST_CONFIG_NAMES
        for relative in _tracked_paths(repo_root, f"*{name}")
        if PurePosixPath(relative).name == name
    )


def _shell_pythonpath_lines(lines: list[str]) -> list[int]:
    return [
        number
        for number, line in enumerate(lines, start=1)
        if not line.lstrip().startswith("#") and _SHELL_PYTHONPATH_RE.search(line)
    ]


def _shell_statement_spans(lines: list[str]) -> list[tuple[int, int]]:
    """The line spans of commands continued over several lines by a trailing backslash."""
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for number, line in enumerate(lines, start=1):
        if start is None:
            start = number
        if not line.rstrip().endswith("\\"):
            if number > start:
                spans.append((start, number))
            start = None
    return spans


def _import_path_files(repo_root: Path) -> list[str]:
    """The files an import-path edit, or a marker, can be in: the template's Python, its pytest
    configs (whose ``pythonpath`` adds to the path for every test under them), and its shell
    scripts."""
    return [
        *_tracked_python_files(repo_root),
        *_tracked_pytest_configs(repo_root),
        *_tracked_paths(repo_root, "*.sh"),
    ]


def _import_path_sites_in(repo_root: Path, relative: str) -> list[_Site]:
    text = (repo_root / relative).read_text(errors="replace")
    lines = text.splitlines()
    if relative.endswith(".py"):
        if not _may_edit_import_path(text):
            return []
        tree = ast.parse(text)
        flagged = _sys_path_edit_lines(tree)
        spans = _statement_spans(tree) if flagged else []
    elif relative.endswith(".sh"):
        flagged, spans = _shell_pythonpath_lines(lines), _shell_statement_spans(lines)
    else:
        flagged = [
            number
            for number, line in enumerate(lines, start=1)
            if _PYTHONPATH_SETTING_RE.match(line)
        ]
        spans = []
    return _sites(relative, lines, flagged, spans, _IMPORT_PATH_MARKER)


def _import_path_sites(repo_root: Path) -> list[_Site]:
    return [
        site
        for relative in _import_path_files(repo_root)
        for site in _import_path_sites_in(repo_root, relative)
    ]


def _sys_path_editors(sites: list[_Site]) -> list[str]:
    """The files with an import-path edit no marker keeps."""
    return sorted({site.relative for site in sites if not site.is_kept()})


_IMPORT_PATH_GUIDE = """\
Remove each edit by installing the package it reaches for in the venv that runs the code,
and run a shell script's python through that venv. You may restructure the code involved,
even when it is not the code you came to change (.agents/shared/references/running-python.md).
An edit that has to stay takes `# import-path: <reason> -- <why>` in the comment lines
directly above it, with <reason> one of: {reasons}, and a <why> as running-python.md
describes it. Name every marker you add in your report.
Never edit this test to let a site through.
"""


def test_prevent_sys_path_edits() -> None:
    sites = _import_path_sites(REPO_ROOT)
    assert not _sys_path_editors(sites), (
        "These change where imports are looked up (sys.path, PYTHONPATH in Python or a shell "
        "script, or a pytest pythonpath setting):\n"
        + "\n".join(site.describe() for site in sites if not site.is_kept())
        + "\n\n"
        + _IMPORT_PATH_GUIDE.format(
            reasons=", ".join(sorted(_REASONS_BY_MARKER[_IMPORT_PATH_MARKER]))
        )
    )


def test_a_listed_third_party_skill_is_not_counted_whatever_its_layout(
    tmp_path: Path,
) -> None:
    edit = "import sys\nsys.path.insert(0, 'x')\n"
    for skill in ("ours", "installed"):
        (tmp_path / ".agents" / "skills" / skill / "scripts").mkdir(parents=True)
        (tmp_path / ".agents" / "skills" / skill / "scripts" / "run.py").write_text(
            edit
        )
    (tmp_path / ".agents" / "skills" / "installed" / "python").mkdir()
    (
        tmp_path / ".agents" / "skills" / "installed" / "python" / "pyproject.toml"
    ).write_text("[project]\n")
    (tmp_path / ".agents" / "third-party-skills.toml").write_text(
        '[skills]\ninstalled = "https://example.com/skills"\n'
    )
    (tmp_path / "system").mkdir()
    (tmp_path / "system" / "tool.py").write_text(edit)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)

    assert _sys_path_editors(_import_path_sites(tmp_path)) == [
        ".agents/skills/ours/scripts/run.py",
        "system/tool.py",
    ]


def test_the_sys_path_scan_sees_each_form_of_edit() -> None:
    assert _edits_sys_path("import sys\nsys.path.insert(0, 'x')\n")
    assert _edits_sys_path("import sys\nsys.path.append('x')\n")
    assert _edits_sys_path("import sys\nsys.path[:0] = ['x']\n")
    assert _edits_sys_path("import sys\nsys.path += ['x']\n")
    assert _edits_sys_path("import sys as _sys\n_sys.path.insert(0, 'x')\n")
    assert _edits_sys_path("from sys import path\npath.append('x')\n")
    assert _edits_sys_path(
        "def test(monkeypatch):\n    monkeypatch.syspath_prepend('x')\n"
    )
    assert _edits_sys_path("import site\nsite.addsitedir('x')\n")
    assert _edits_sys_path(
        "import subprocess\nsubprocess.run(['x'], env={'PYTHONPATH': 'x'})\n"
    )
    assert _edits_sys_path(
        "import os\nenv = dict(os.environ)\nenv['PYTHONPATH'] = 'x'\n"
    )
    assert _edits_sys_path("import os\nenv = dict(os.environ, PYTHONPATH='x')\n")
    assert not _edits_sys_path("import sys\nprint(sys.path)\n")
    assert not _edits_sys_path(
        "import os\nenv = dict(os.environ)\nenv.pop('PYTHONPATH', None)\n"
    )
    assert not _edits_sys_path(
        "import subprocess\nsubprocess.run(['x'], env={'PYTHONPATH': ''})\n"
    )


def test_a_pytest_pythonpath_setting_is_counted(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npythonpath = ["src"]\n'
    )
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["."]\n'
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)

    assert _sys_path_editors(_import_path_sites(tmp_path)) == ["app/pyproject.toml"]


def _file_relative_path(node: ast.expr, file: Path) -> Path | None:
    """Where ``Path(__file__)``-based path arithmetic points, or None for any other expression."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and len(node.args) == 1
    ):
        if node.func.id == "str":
            return _file_relative_path(node.args[0], file)
        if (
            node.func.id == "Path"
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "__file__"
        ):
            return file
        return None
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "resolve"
    ):
        inner = _file_relative_path(node.func.value, file)
        return inner.resolve() if inner is not None else None
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        inner = _file_relative_path(node.value, file)
        return inner.parent if inner is not None else None
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "parents"
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, int)
    ):
        inner = _file_relative_path(node.value.value, file)
        return inner.parents[node.slice.value] if inner is not None else None
    if (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Div)
        and isinstance(node.right, ast.Constant)
    ):
        inner = _file_relative_path(node.left, file)
        return inner / str(node.right.value) if inner is not None else None
    return None


def _sys_path_insert_targets(source: str, file: Path) -> list[tuple[int, Path | None]]:
    """``(line, directory)`` for each ``sys.path.insert``/``append``; None where it cannot be read."""
    targets: list[tuple[int, Path | None]] = []
    for node in ast.walk(ast.parse(source)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("insert", "append")
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "path"
            and isinstance(node.func.value.value, ast.Name)
            and node.func.value.value.id == "sys"
            and node.args
        ):
            continue
        added = _file_relative_path(node.args[-1], file)
        targets.append((node.lineno, added.resolve() if added is not None else None))
    return targets


def _lets_an_edit_leave_the_bare_search_paths(site: _Site, runs_bare: bool) -> bool:
    """Code the system python3 runs is type-checked against exactly the bare search paths, so
    there only an ``other-tree`` edit, whose directory is known only at run time, may leave them."""
    return any(
        marker.is_valid() and (not runs_bare or marker.reason == "other-tree")
        for marker in site.markers
    )


def _sys_path_edits_outside_the_bare_search_paths(
    repo_root: Path, allowed: tuple[Path, ...]
) -> list[str]:
    resolved_allowed = {path.resolve() for path in allowed}
    offenders: list[str] = []
    for relative in _tracked_python_files(repo_root):
        source = (repo_root / relative).read_text()
        outside = [
            (line, target)
            for line, target in _sys_path_insert_targets(source, repo_root / relative)
            if target not in resolved_allowed
        ]
        if not outside:
            continue
        runs_bare = is_bare_entry(repo_root / relative)
        kept = {
            site.line
            for site in _sites(
                relative,
                source.splitlines(),
                [line for line, _ in outside],
                _statement_spans(ast.parse(source)),
                _IMPORT_PATH_MARKER,
            )
            if _lets_an_edit_leave_the_bare_search_paths(site, runs_bare)
        }
        offenders.extend(
            f"{relative}:{line}: adds {target if target is not None else 'a path this check cannot read'}"
            for line, target in outside
            if line not in kept
        )
    return offenders


def test_sys_path_edits_only_add_a_bare_search_path() -> None:
    offenders = _sys_path_edits_outside_the_bare_search_paths(
        REPO_ROOT, BARE_SEARCH_PATHS
    )
    assert not offenders, (
        "A sys.path edit may only add a bare search path (entry_points_testing.BARE_SEARCH_PATHS: "
        "where the bare packages and the stdlib-only libraries live), written as path arithmetic on "
        "Path(__file__) so the bare type check, which searches exactly those directories, resolves "
        "what the runtime imports, unless an `# import-path: <reason> -- <why>` marker keeps it; in "
        "code the system python3 runs, only an `other-tree` marker does "
        "(.agents/shared/references/running-python.md):\n" + "\n".join(offenders)
    )


def test_the_sys_path_target_check_reads_file_relative_paths_and_refuses_others(
    tmp_path: Path,
) -> None:
    allowed_dir = tmp_path / "system" / "scripts"
    allowed_dir.mkdir(parents=True)
    module = (
        tmp_path
        / ".agents"
        / "skills"
        / "probe"
        / "python"
        / "probe_skill"
        / "probe.py"
    )
    module.parent.mkdir(parents=True)
    (module.parent.parent / "pyproject.toml").write_text("[project]\n")
    module.write_text(
        "import sys\nfrom pathlib import Path\n\n"
        "if not __package__:\n"
        '    sys.path.insert(0, str(Path(__file__).resolve().parents[5] / "system" / "scripts"))\n'
        'sys.path.append(str(Path(__file__).resolve().parent / "elsewhere"))\n'
        "sys.path.insert(0, computed_somewhere)\n"
        "# import-path: other-tree -- the target release's tree, known only at run time\n"
        "sys.path.insert(0, target_tree)\n"
        "# import-path: no-venv -- `uv run --no-project` runs this with no venv\n"
        'sys.path.insert(0, str(Path(__file__).resolve().parent / "beside"))\n'
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)

    offenders = _sys_path_edits_outside_the_bare_search_paths(tmp_path, (allowed_dir,))

    relative = ".agents/skills/probe/python/probe_skill/probe.py"
    assert offenders == [
        f"{relative}:6: adds {(module.parent / 'elsewhere').resolve()}",
        f"{relative}:7: adds a path this check cannot read",
    ]


def test_only_an_other_tree_marker_lets_bare_code_leave_the_bare_search_paths(
    tmp_path: Path,
) -> None:
    allowed_dir = tmp_path / "system" / "scripts"
    package = allowed_dir / "workspace_bare_scripts"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "probe.py").write_text(
        "import sys\nfrom pathlib import Path\n\n"
        "# import-path: no-venv -- the system python3 runs this with no venv\n"
        'sys.path.insert(0, str(Path(__file__).resolve().parent / "elsewhere"))\n'
        "# import-path: other-tree -- the target release's tree, known only at run time\n"
        "sys.path.insert(0, target_tree)\n"
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)

    offenders = _sys_path_edits_outside_the_bare_search_paths(tmp_path, (allowed_dir,))

    assert offenders == [
        f"system/scripts/workspace_bare_scripts/probe.py:5: adds {(package / 'elsewhere').resolve()}"
    ]


# The ways to run a module from a file instead of importing it: each one sidesteps the
# packages, so what it loads is a second copy of the module under another name.
_PATH_LOADING_RE = re.compile(
    r"\b(?:spec_from_file_location|spec_from_loader|SourceFileLoader|SourcelessFileLoader|load_source|run_path)\(|\bexec\(\s*compile\("
)


def _path_loading_lines(text: str) -> list[tuple[int, str]]:
    return [
        (number, line)
        for number, line in enumerate(text.splitlines(), start=1)
        if _PATH_LOADING_RE.search(line)
    ]


def _path_loading_sites(repo_root: Path) -> list[str]:
    return [
        f"{relative}:{number}: {line.strip()}"
        for relative in _tracked_python_files(repo_root)
        for number, line in _path_loading_lines(
            (repo_root / relative).read_text(errors="replace")
        )
    ]


def test_prevent_loading_modules_by_file_path() -> None:
    sites = _path_loading_sites(REPO_ROOT)
    assert len(sites) <= snapshot(6), (
        "These load a module from a file path. Import it instead: a skill's modules live in its "
        "python/ project and a system script's beside it, all importable from the root venv:\n"
        + "\n".join(sites)
    )


def test_the_path_loading_scan_sees_each_form() -> None:
    text = "\n".join(
        [
            "spec = importlib.util.spec_from_file_location('m', path)",
            "loader = importlib.machinery.SourceFileLoader('m', path)",
            "runpy.run_path(path, run_name='__main__')",
            "loader = importlib.machinery.SourcelessFileLoader('m', path)",
            "spec = importlib.util.spec_from_loader('m', loader)",
            "exec(compile(source, path, 'exec'), namespace)",
            "from workspace_bare_scripts import message_chat",
            "importlib.import_module('pcmflux')",
        ]
    )

    assert [number for number, _ in _path_loading_lines(text)] == [1, 2, 3, 4, 5, 6]


def _user_package_scripts(repo_root: Path) -> dict[str, str]:
    """Each user package's console scripts, by script name, mapped to its directory."""
    return {
        script: relative
        for relative in user_packages.user_packages(repo_root)
        for script in user_packages.console_scripts(repo_root / relative)
    }


def _root_venv_runs_of_user_scripts(
    repo_root: Path, files: list[Path]
) -> list[tuple[str, str]]:
    """Every ``(file, line)`` that runs a user package's command with ``uv run`` from the
    root project: no ``--project`` or ``--directory``, outside the package's own directory."""
    scripts = _user_package_scripts(repo_root)
    found: list[tuple[str, str]] = []
    if not scripts:
        return found
    for path in files:
        relative = path.relative_to(repo_root).as_posix()
        for line in path.read_text(errors="replace").splitlines():
            for match in _UV_RUN_RE.finditer(line):
                if "--project" in match.group(0) or "--directory" in match.group(0):
                    continue
                words = [
                    word.strip("`'\"()*,;:") for word in line[match.end() :].split()
                ]
                index = 0
                while index < len(words) and words[index].startswith("--"):
                    if words[index] in ("--project", "--directory"):
                        break
                    index += 2 if words[index] in _OPTIONS_WITH_VALUES else 1
                else:
                    if index < len(words) and words[index] in scripts:
                        package = scripts[words[index]]
                        if not relative.startswith(package + "/"):
                            found.append((relative, line.strip()))
    return found


def test_no_one_off_runs_a_user_packages_command_from_the_root_venv() -> None:
    assert _root_venv_runs_of_user_scripts(REPO_ROOT, _scanned_files()) == []


def test_a_root_venv_run_of_a_user_packages_command_is_caught(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.uv.workspace]\nmembers = ["system/apps/chat"]\n'
    )
    widget = tmp_path / "system" / "apps" / "widget"
    widget.mkdir(parents=True)
    (widget / "pyproject.toml").write_text(
        '[project]\nname = "widget"\n\n[project.scripts]\nwidget-report = "widget.cli:main"\n'
    )
    readme = widget / "README.md"
    readme.write_text("Run `uv run --no-sync widget-report` here.\n")
    skill = tmp_path / ".agents" / "skills" / "report" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "Run `uv run --no-sync widget-report --today`.\n"
        "Or `uv run --no-sync --project system/apps/widget widget-report`.\n"
        "Or `uv run --no-sync other-tool`.\n"
    )

    assert _root_venv_runs_of_user_scripts(tmp_path, [readme, skill]) == [
        (
            ".agents/skills/report/SKILL.md",
            "Run `uv run --no-sync widget-report --today`.",
        )
    ]


class _OwnPrograms(NamedTuple):
    """What a launch of the workspace's own code names: a runnable module's path, a package's module after
    ``-m``, or a command one of its packages declares."""

    scripts: frozenset[str]
    packages: frozenset[str]
    commands: frozenset[str]


def _own_programs(repo_root: Path) -> _OwnPrograms:
    commands: set[str] = set()
    for relative in _tracked_paths(repo_root, "*pyproject.toml"):
        project = tomllib.loads((repo_root / relative).read_text()).get("project", {})
        commands.update(project.get("scripts", {}))
    return _OwnPrograms(
        scripts=frozenset(
            relative
            for relative in _tracked_paths(repo_root, "*.py")
            if not is_test_file(Path(relative))
            and is_entry_module(repo_root / relative)
        ),
        packages=frozenset(
            PurePosixPath(relative).parent.name
            for relative in _tracked_paths(repo_root, "*/__init__.py")
        ),
        commands=frozenset(commands),
    )


def _names_own_script(
    written: str, scripts: frozenset[str], *, is_path_part: bool
) -> bool:
    """Whether ``written`` is a path to one of ``scripts``: repo-relative, absolute, or a tail of one.

    A file name alone names a script only as the last part of a path expression (``base / "x.py"``,
    ``.with_name("x.py")``) and only when no other script shares it: a bare string is how the guard checks spell
    a script they look for in a command, not one they run.
    """
    if not written.endswith(".py") or any(character.isspace() for character in written):
        return False
    written = written.removeprefix("./")
    if "/" not in written:
        return (
            is_path_part
            and sum(PurePosixPath(script).name == written for script in scripts) == 1
        )
    return any(
        script.endswith(f"/{written}")
        or written.endswith(f"/{script}")
        or script == written
        for script in scripts
    )


def _joined_path(node: ast.BinOp) -> str:
    """The literal parts of a ``base / "a" / "b.py"`` chain, joined; an unknown base contributes nothing."""
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.BinOp) and isinstance(current.op, ast.Div):
        if isinstance(current.right, ast.Constant) and isinstance(
            current.right.value, str
        ):
            parts.insert(0, current.right.value)
        current = current.left
    if isinstance(current, ast.Constant) and isinstance(current.value, str):
        parts.insert(0, current.value)
    return "/".join(parts)


# Programs that run the argv after them (``timeout 60 tool-cmd``), past options and values
# this scan does not parse.
_ARGV_RUNNERS = frozenset(
    {
        "chrt",
        "env",
        "exec",
        "flock",
        "ionice",
        "nice",
        "nohup",
        "setsid",
        "stdbuf",
        "sudo",
        "taskset",
        "timeout",
        "xargs",
    }
)


def _command_word(words: list[object]) -> str | None:
    """The program an argv runs: its first word, or the command ``uv run`` runs past uv's and
    run's options; None when it cannot be read: a word that is not a literal stands where it
    would be, the first word is a program that runs the argv after it, or the word follows a
    run option that may take it as its value."""
    if not words or not isinstance(words[0], str) or words[0] in _ARGV_RUNNERS:
        return None
    if words[0] != "uv":
        return words[0]
    index = _past_options(words, 1, _UV_GLOBAL_OPTIONS_WITH_VALUES)
    if index < len(words) and not isinstance(words[index], str):
        return None
    if index >= len(words) or words[index] != "run":
        return words[0]
    start = index + 1
    index = _past_options(words, start, _OPTIONS_WITH_VALUES)
    previous = words[index - 1] if index > start else None
    if (
        isinstance(previous, str)
        and previous.startswith("-")
        and previous not in _OPTIONS_WITH_VALUES | _NON_SYNCING_OPTIONS | {"--"}
        and "=" not in previous
    ):
        # An option this scan does not know may take the word after it as its value.
        return None
    command = words[index] if index < len(words) else None
    return command if isinstance(command, str) else None


def _names_own_program(node: ast.expr, programs: _OwnPrograms) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _names_own_script(node.value, programs.scripts, is_path_part=False)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _names_own_script(
            _joined_path(node), programs.scripts, is_path_part=True
        )
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "with_name"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return _names_own_script(
            node.args[0].value, programs.scripts, is_path_part=True
        )
    # A list or tuple holding another is a record (a route table's row), not a command line.
    if isinstance(node, (ast.List, ast.Tuple)) and not any(
        isinstance(element, (ast.List, ast.Tuple)) for element in node.elts
    ):
        words: list[object] = [
            element.value if isinstance(element, ast.Constant) else None
            for element in node.elts
        ]
        # Behind a word that is not a literal (``[*LAUNCHER, "tool-cmd"]``,
        # ``[UV, "run", "tool-cmd"]``, ``["uv", "run", *FLAGS, "tool-cmd"]``) the program the
        # argv runs cannot be read, so any console-script name in it may be that program.
        command = _command_word(words)
        return (
            any(
                word == "-m"
                and isinstance(after, str)
                and after.split(".")[0] in programs.packages
                for word, after in zip(words, words[1:])
            )
            or command in programs.commands
            or (command is None and any(word in programs.commands for word in words))
        )
    return False


# Methods that only write, read or check a file: a path they are called on names that
# file, not a program to run.
_FILE_ONLY_METHODS = frozenset(
    {
        "write_text",
        "write_bytes",
        "read_text",
        "read_bytes",
        "open",
        "exists",
        "is_file",
        "is_dir",
        "stat",
    }
)


def _program_launch_lines(tree: ast.AST, programs: _OwnPrograms) -> list[int]:
    file_only = {
        id(part)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _FILE_ONLY_METHODS
        for part in ast.walk(node.func.value)
    }
    return sorted(
        {
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.expr)
            and id(node) not in file_only
            and _names_own_program(node, programs)
        }
    )


def _is_test_support(relative: str) -> bool:
    return is_test_file(Path(relative)) or PurePosixPath(relative).name.endswith(
        "testing.py"
    )


def _launch_sites_in(
    repo_root: Path, relative: str, programs: _OwnPrograms
) -> list[_Site]:
    """The launch sites in ``relative``, marked or not. The file's own path is not one of the
    scripts matched: a file naming itself (a usage hint it prints) is not calling other
    workspace code."""
    source = (repo_root / relative).read_text()
    tree = ast.parse(source)
    others = programs._replace(scripts=programs.scripts - {relative})
    return _sites(
        relative,
        source.splitlines(),
        _program_launch_lines(tree, others),
        _statement_spans(tree),
        _OWN_PROCESS_MARKER,
    )


def _launch_sites(repo_root: Path) -> list[_Site]:
    programs = _own_programs(repo_root)
    return [
        site
        for relative in _tracked_python_files(repo_root)
        if not _is_test_support(relative)
        for site in _launch_sites_in(repo_root, relative, programs)
    ]


def _program_launch_sites(repo_root: Path) -> list[str]:
    """The launches no marker keeps."""
    return [site.describe() for site in _launch_sites(repo_root) if not site.is_kept()]


_LAUNCH_GUIDE = """\
Turn each site into an import: declare the package it runs in the caller's pyproject.toml
and call its functions. You may restructure the caller and the code it runs, even when it
is not the code you came to change (.agents/shared/references/running-python.md, "Calling
workspace code from Python"). A site that needs a process of its own takes
`# own-process: <reason> -- <why>` in the comment lines directly above it, with <reason>
one of: {reasons}, and a <why> as running-python.md describes it. Name every marker you
add in your report. Never edit this test to let a site through.
"""


def test_prevent_running_the_workspaces_own_code_as_a_process() -> None:
    sites = _program_launch_sites(REPO_ROOT)
    assert not sites, (
        "These name one of the workspace's own scripts, modules or commands to run it as "
        "a process:\n"
        + "\n".join(sites)
        + "\n\n"
        + _LAUNCH_GUIDE.format(
            reasons=", ".join(sorted(_REASONS_BY_MARKER[_OWN_PROCESS_MARKER]))
        )
    )


def _marker_problems(repo_root: Path) -> list[str]:
    """Markers that keep nothing: an unknown reason, no why, or no site of their kind below.

    Only a file holding a marker has its sites read, so a tree with few markers costs a text scan.
    """
    programs: _OwnPrograms | None = None
    problems: list[str] = []
    for relative in _import_path_files(repo_root):
        markers = _markers(
            (repo_root / relative).read_text(errors="replace").splitlines()
        )
        if not markers:
            continue
        sites = _import_path_sites_in(repo_root, relative)
        if relative.endswith(".py") and not _is_test_support(relative):
            if programs is None:
                programs = _own_programs(repo_root)
            sites += _launch_sites_in(repo_root, relative, programs)
        attached = {marker.line for site in sites for marker in site.markers}
        for marker in markers:
            where = f"{relative}:{marker.line}: {marker.kind}"
            if not marker.is_valid():
                problems.append(
                    f"{where}: needs `<reason> -- <why>`, <reason> one of "
                    + ", ".join(sorted(_REASONS_BY_MARKER[marker.kind]))
                )
            elif marker.line not in attached:
                problems.append(
                    f"{where}: keeps nothing; it belongs in the comment lines directly "
                    "above the statement it keeps"
                )
    return problems


def test_every_marker_keeps_a_site_for_a_named_reason() -> None:
    assert _marker_problems(REPO_ROOT) == []


def test_the_launch_scan_sees_each_way_of_naming_an_own_program(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "tool"\n\n[project.scripts]\ntool-cmd = "pkg.tool:main"\n'
    )
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    main_guard = 'if __name__ == "__main__":\n    pass\n'
    (package / "tool.py").write_text('USAGE_PATH = "pkg/tool.py"\n' + main_guard)
    (package / "helpers.py").write_text("")
    (package / "caller.py").write_text(
        "\n".join(
            [
                "import subprocess, sys",
                "from pathlib import Path",
                'subprocess.run([sys.executable, "pkg/tool.py"])',
                'TOOL = Path("pkg") / "tool.py"',
                'subprocess.run([sys.executable, "-m", "pkg.tool"])',
                'subprocess.run(["uv", "run", "--no-sync", "tool-cmd"])',
                'TOOL_BY_NAME = ROOT / "tool.py"',
                'SIBLING = Path(__file__).with_name("tool.py")',
                'HELPERS = "pkg/helpers.py"',
                'GUARDED_NAME = "tool.py"',
                'print("run python3 pkg/tool.py by hand")',
                'subprocess.run([sys.executable, "-m", "pytest"])',
                'subprocess.run(["mngr", "list"])',
                '(ROOT / "tool.py").write_text("")',
                'Path("pkg/tool.py").write_bytes(b"")',
                'subprocess.run(["tool-cmd", "--today"])',
                'subprocess.run(["supervisorctl", "status", "tool-cmd"])',
                'ROUTES = [("tool-cmd", handler, ("GET",))]',
                'IS_ROOT = (ROOT / "pkg" / "tool.py").exists()',
                'SOURCE = Path("pkg/tool.py").read_text()',
                'subprocess.run(["uv", "--project", "pkg", "run", "--no-sync", "tool-cmd"])',
                'subprocess.run([*UV_RUN, "tool-cmd"])',
                'subprocess.run([UV, "run", "--no-sync", "tool-cmd"])',
                'subprocess.run(["uv", "run", *FLAGS, "tool-cmd"])',
                'subprocess.run(["uv", *GLOBAL_OPTIONS, "run", "tool-cmd"])',
                'subprocess.run(["timeout", "60", "tool-cmd"])',
                'subprocess.run(["uv", "run", "-p", "3.12", "tool-cmd"])',
            ]
        )
    )
    (package / "caller_test.py").write_text(
        'import subprocess, sys\nsubprocess.run([sys.executable, "pkg/tool.py"])\n'
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)

    sites = _program_launch_sites(tmp_path)

    assert [site.split(": ", 1)[0] for site in sites] == [
        "pkg/caller.py:3",
        "pkg/caller.py:4",
        "pkg/caller.py:5",
        "pkg/caller.py:6",
        "pkg/caller.py:7",
        "pkg/caller.py:8",
        "pkg/caller.py:16",
        "pkg/caller.py:21",
        "pkg/caller.py:22",
        "pkg/caller.py:23",
        "pkg/caller.py:24",
        "pkg/caller.py:25",
        "pkg/caller.py:26",
        "pkg/caller.py:27",
    ]


def _git_tree(root: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(text)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)


def test_a_marker_keeps_the_statement_below_it_for_a_named_reason(
    tmp_path: Path,
) -> None:
    _git_tree(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/tool.py": 'if __name__ == "__main__":\n    pass\n',
            "pkg/caller.py": "\n".join(
                [
                    "import subprocess",
                    "# Runs the wrapper, which execs the command it is given.",
                    "# own-process: wrapper -- hands the secret to the child only",
                    "subprocess.run(",
                    '    ["python3", "pkg/tool.py", "--", "node"],',
                    ")",
                    "# own-process: faster -- not a reason the rules give",
                    'UNKNOWN_REASON = "pkg/tool.py"',
                    "# own-process: deadline",
                    'NO_WHY = "pkg/tool.py"',
                    "# own-process: deadline -- separated from its statement",
                    "",
                    'SEPARATED = "pkg/tool.py"',
                    "# own-process: server -- nothing below names a program",
                    "VALUE = 1",
                    "# own-process: wrapper hands the secret to the child only",
                    'NO_SEPARATOR = "pkg/tool.py"',
                ]
            ),
        },
    )

    assert [site.split(": ", 1)[0] for site in _program_launch_sites(tmp_path)] == [
        "pkg/caller.py:8",
        "pkg/caller.py:10",
        "pkg/caller.py:13",
        "pkg/caller.py:17",
    ]
    assert [problem.split(": ", 1)[0] for problem in _marker_problems(tmp_path)] == [
        "pkg/caller.py:7",
        "pkg/caller.py:9",
        "pkg/caller.py:11",
        "pkg/caller.py:14",
        "pkg/caller.py:16",
    ]


def test_import_path_edits_count_per_file_and_a_marker_keeps_one(
    tmp_path: Path,
) -> None:
    _git_tree(
        tmp_path,
        {
            "tools/bare.py": "\n".join(
                [
                    "import sys",
                    "# import-path: no-venv -- the system python3 runs this with no venv",
                    "sys.path.insert(0, 'lib')",
                ]
            ),
            "tools/loose.py": "import sys\nsys.path.insert(0, 'lib')\n",
            "pipeline/refresh.sh": "\n".join(
                [
                    "#!/bin/bash",
                    "# PYTHONPATH=lib python3 -m tool in a comment is not a run",
                    "PYTHONPATH= python3 -m clean_env",
                    'PYTHONPATH="" python3 -m clean_env',
                    'echo "$PYTHONPATH"',
                    "PYTHONPATH=lib/src python3 -m tool export",
                ]
            ),
            "pipeline/marked.sh": "\n".join(
                [
                    "# import-path: other-tree -- the target release's copy, found at run time",
                    'export PYTHONPATH="$TARGET/lib"',
                    "# import-path: other-tree -- the same copy, set on a continued line",
                    "env LANG=C \\",
                    '    PYTHONPATH="$TARGET/lib" python3 -m tool',
                ]
            ),
            "app/pyproject.toml": '[tool.pytest.ini_options]\npythonpath = ["src"]\n',
            "lib/pyproject.toml": "\n".join(
                [
                    "[tool.pytest.ini_options]",
                    "# import-path: no-venv -- its tests run under the system python3",
                    'pythonpath = ["src"]',
                ]
            ),
        },
    )

    assert _sys_path_editors(_import_path_sites(tmp_path)) == [
        "app/pyproject.toml",
        "pipeline/refresh.sh",
        "tools/loose.py",
    ]
    assert [
        site.describe()
        for site in _import_path_sites(tmp_path)
        if site.relative == "pipeline/refresh.sh"
    ] == ["pipeline/refresh.sh:6: PYTHONPATH=lib/src python3 -m tool export"]
    assert _marker_problems(tmp_path) == []
