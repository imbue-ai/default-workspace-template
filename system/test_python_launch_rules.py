"""How Python gets launched in this workspace (see .agents/shared/references/running-python.md).

Two rules, each checked against the real tree and against a planted violation:

- A long-running program never runs under ``uv run``, so no ``uv`` process stays
  resident as its parent: this covers supervisord programs (built-in and user-added
  drop-ins alike) and the tmux windows ``.mngr/settings.toml`` opens. One that starts
  a root-venv entry point syncs ``--frozen`` and then runs ``exec .venv/bin/<name>``.
- A one-off written into an agent-facing file, or into a string or argv in the
  workspace's Python, runs ``uv run --no-sync``, so following an instruction can
  never relock or sync the workspace behind the agent's back.
"""

from __future__ import annotations

import ast
import configparser
import re
import shlex
import subprocess
import tomllib
from pathlib import Path, PurePosixPath

from imbue.mngr.interfaces.host import NamedCommand

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SUPERVISORD_DIR = _REPO_ROOT / "system"
_MNGR_SETTINGS = _REPO_ROOT / ".mngr" / "settings.toml"

_SHELL_PROGRAMS = frozenset({"bash", "sh", "zsh", "dash"})
_EXTRA_WINDOW_KEYS = frozenset(
    {"extra_window", "extra_window__extend", "extra_window__assign"}
)

# The file types agents read commands out of (docs, prompts, config, scripts).
_AGENT_FACING_SUFFIXES = (".md", ".toml", ".sh", ".conf")
# Path parts whose files are records, third-party code or test inputs, not instructions.
_EXEMPT_PARTS = frozenset({"changelog", "vendor", "fixtures"})
_EXEMPT_NAMES = frozenset({"CHANGELOG.md", "UNABRIDGED_CHANGELOG.md"})
# Dated design records under docs/, which quote the commands of their day. The living
# contracts among them are what agents are pointed at as the rules, so those are scanned.
_DOCS_ROOT = "docs"
_DESIGN_RECORD_PARTS = frozenset({"blueprint", "specs"})
_LIVING_CONTRACT_NAME = "contracts.md"

_UV_RUN_RE = re.compile(r"\buv run\b")
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

# Deliberate syncing ``uv run`` uses: (repo-relative path, a substring of the line).
_DELIBERATE_SYNCS: tuple[tuple[str, str], ...] = (
    # The update worker reads footprints and test selections with the app-manifest
    # library as the merge left it, so it syncs that package from the merged lock first.
    (
        ".agents/skills/update-self/references/update-self-worker.md",
        "uv run --frozen --package app-manifest app-manifest",
    ),
)


def _shell_command_string(words: list[str], shell_index: int) -> str | None:
    """The command string a shell at ``shell_index`` runs (``-c``, ``-lc``, ``--login -c``), or None."""
    for index in range(shell_index + 1, len(words)):
        word = words[index]
        if word.startswith("--"):
            continue
        if not word.startswith("-"):
            return None
        if "c" in word[1:]:
            return words[index + 1] if index + 1 < len(words) else None
    return None


def _runs_uv_run(command: str) -> bool:
    """Whether a program command executes ``uv run``, directly or inside a ``bash -c`` string."""
    words = shlex.split(command)
    for index, word in enumerate(words):
        if PurePosixPath(word).name == "uv" and words[index + 1 : index + 2] == ["run"]:
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
    not an invocation. A script path stays exempt until the skill and system scripts
    become packages.
    """
    if not rest.startswith(" "):
        return True
    words = [word.strip("`'\"()*,.;:") for word in rest.split()]
    index = 0
    while index < len(words) and words[index].startswith("--"):
        if words[index] in _NON_SYNCING_OPTIONS:
            return True
        index += 2 if words[index] in _OPTIONS_WITH_VALUES else 1
    program = words[index : index + 2]
    if program and program[0] in {"python", "python3"}:
        program = program[1:]
    return bool(program) and (program[0].endswith(".py") or program[0].startswith("$"))


def _plain_uv_runs(text: str) -> list[tuple[int, str]]:
    """Every ``(line number, line)`` in ``text`` with a plain, syncing ``uv run``."""
    return [
        (number, line)
        for number, line in enumerate(text.splitlines(), start=1)
        for match in _UV_RUN_RE.finditer(line)
        if not _is_compliant_uv_run(line[match.end() :])
    ]


def _argv_runs_plain_uv_run(elements: list[ast.expr]) -> bool:
    """Whether a list or tuple literal is a ``uv run`` argv whose literal words make it a syncing run."""
    literals: list[str] = []
    for element in elements:
        if not (isinstance(element, ast.Constant) and isinstance(element.value, str)):
            break
        literals.append(element.value)
    if literals[:2] != ["uv", "run"] or len(literals) < 3:
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


def _is_scanned(relative: str) -> bool:
    path = PurePosixPath(relative)
    if _EXEMPT_PARTS.intersection(path.parts) or path.name in _EXEMPT_NAMES:
        return False
    if (
        path.parts[0] == _DOCS_ROOT
        and _DESIGN_RECORD_PARTS.intersection(path.parts)
        and path.name != _LIVING_CONTRACT_NAME
    ):
        return False
    if path.suffix == ".py":
        return not _is_test_source(path)
    return relative.endswith(_AGENT_FACING_SUFFIXES)


def _scanned_files() -> list[Path]:
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    return [
        _REPO_ROOT / relative
        for relative in tracked
        if relative and _is_scanned(relative) and (_REPO_ROOT / relative).is_file()
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
        violations.extend(
            (str(path.relative_to(_REPO_ROOT)), line) for _, line in found
        )
    return violations


def _is_deliberate(relative: str, line: str) -> bool:
    return any(
        relative == path and context in line for path, context in _DELIBERATE_SYNCS
    )


def test_no_supervisord_program_runs_under_uv_run() -> None:
    offenders = _programs_under_uv_run(_SUPERVISORD_DIR)
    assert not offenders, (
        "These supervisord programs run under `uv run`, which stays resident as their parent and can "
        'relock the workspace. Use `bash -c "uv sync --all-packages --frozen && exec .venv/bin/<name>"` '
        "(see .agents/shared/references/running-python.md):\n" + "\n".join(offenders)
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
    (dropins / "fine.conf").write_text(
        "; never `command=uv run fine`: uv would stay resident as its parent\n"
        '[program:fine]\ncommand=bash -c "uv sync --all-packages --frozen && exec .venv/bin/fine"\n'
    )

    offenders = _programs_under_uv_run(tmp_path)

    assert [offender.split("]", 1)[0] for offender in offenders] == [
        "[program:direct",
        "[program:login",
        "[program:longopt",
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
        ]
    )

    assert [number for number, _ in _plain_uv_runs(text)] == [1, 2, 5]


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

    assert [number for number, _ in _python_plain_uv_runs(source)] == [1, 2]


def test_scanning_skips_records_and_tests_but_reaches_living_contracts() -> None:
    scanned = {str(path.relative_to(_REPO_ROOT)) for path in _scanned_files()}

    assert not any(
        "/changelog/" in path or path.endswith("CHANGELOG.md") for path in scanned
    )
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
