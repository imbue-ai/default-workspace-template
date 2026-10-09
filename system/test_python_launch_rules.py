"""How Python gets launched in this workspace (see .agents/shared/references/running-python.md).

Each rule is checked against the real tree and against a planted violation:

- A long-running program never runs under ``uv run``: supervisord programs (built-in
  and user-added drop-ins alike) sync ``--frozen`` and then ``exec .venv/bin/<name>``,
  so no ``uv`` process stays resident as their parent.
- A one-off written into an agent-facing file runs ``uv run --no-sync``, so following
  an instruction can never relock or sync the workspace behind the agent's back.
- A ``python3 <path>.py`` written into an agent-facing file names a bare entry point: the
  system ``python3`` has no venv, so a venv-tier script run that way fails at its first
  third-party import.
- Only the bare entry stubs that need a library outside their scripts directory edit
  ``sys.path``; everything else imports packages.
"""

from __future__ import annotations

import ast
import re
import shlex
import subprocess
from pathlib import Path, PurePosixPath

from entry_points_testing import STANDALONE_BARE_DIRS, is_bare_entry, is_test_file

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SUPERVISORD_DIR = _REPO_ROOT / "system"

_SHELL_PROGRAMS = frozenset({"bash", "sh", "zsh", "dash"})

# The file types agents read commands out of (docs, prompts, config, scripts).
_AGENT_FACING_SUFFIXES = (".md", ".toml", ".sh", ".conf")
# Plus the hook configs, for the python3 rule.
_PYTHON3_SCANNED_SUFFIXES = (*_AGENT_FACING_SUFFIXES, ".json")
# Path parts whose files are records or test inputs, not instructions.
_EXEMPT_PARTS = frozenset({"changelog", "blueprint", "specs", "vendor", "fixtures"})
_EXEMPT_NAMES = frozenset({"CHANGELOG.md", "UNABRIDGED_CHANGELOG.md"})

_UV_RUN_RE = re.compile(r"\buv run\b")
# Options after which ``uv run`` neither syncs the workspace venv nor touches uv.lock.
_NON_SYNCING_OPTIONS = frozenset({"--no-sync", "--no-project"})
# uv run options that take a separate value word.
_OPTIONS_WITH_VALUES = frozenset(
    {"--package", "--with", "--env-file", "--python", "--directory", "--project", "--group"}
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
    (".agents/skills/update-self/references/python-packaging-migration.md", "runs `uv run <name>`"),
    (".agents/skills/update-self/references/python-packaging-migration.md", "a plain `uv run X` becomes"),
)


def _runs_uv_run(command: str) -> bool:
    """Whether a program command executes ``uv run``, directly or inside a ``bash -c`` string."""
    words = shlex.split(command)
    for index, word in enumerate(words):
        if PurePosixPath(word).name == "uv" and words[index + 1 : index + 2] == ["run"]:
            return True
        if (
            PurePosixPath(word).name in _SHELL_PROGRAMS
            and words[index + 1 : index + 2] == ["-c"]
            and index + 2 < len(words)
            and _runs_uv_run(words[index + 2])
        ):
            return True
    return False


def _program_commands(conf: Path) -> list[tuple[str, str]]:
    """Every ``(section, command)`` a supervisord config file declares, continuation lines joined."""
    commands: list[tuple[str, str]] = []
    section = ""
    lines = conf.read_text().splitlines()
    for index, line in enumerate(lines):
        if line.startswith("["):
            section = line.strip()
        elif line.startswith("command="):
            parts = [line.removeprefix("command=")]
            for continuation in lines[index + 1 :]:
                if not continuation[:1].isspace() or not continuation.strip():
                    break
                parts.append(continuation.strip())
            commands.append((section, " ".join(parts)))
    return commands


def _programs_under_uv_run(supervisord_dir: Path) -> list[str]:
    confs = [supervisord_dir / "supervisord.conf", *sorted((supervisord_dir / "supervisord.conf.d").glob("*.conf"))]
    return [
        f"{conf.name} {section}: {command}"
        for conf in confs
        if conf.is_file()
        for section, command in _program_commands(conf)
        if _runs_uv_run(command)
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


def _agent_facing_files(suffixes: tuple[str, ...] = _AGENT_FACING_SUFFIXES) -> list[Path]:
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], cwd=_REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.split("\0")
    return [
        _REPO_ROOT / relative
        for relative in tracked
        if relative.endswith(suffixes)
        and not _EXEMPT_PARTS.intersection(PurePosixPath(relative).parts)
        and PurePosixPath(relative).name not in _EXEMPT_NAMES
        and (_REPO_ROOT / relative).is_file()
    ]


def _plain_uv_run_violations() -> list[tuple[str, str]]:
    return [
        (str(path.relative_to(_REPO_ROOT)), line)
        for path in _agent_facing_files()
        for _, line in _plain_uv_runs(path.read_text(errors="replace"))
    ]


def _is_deliberate(relative: str, line: str) -> bool:
    return any(relative == path and context in line for path, context in _DELIBERATE_SYNCS)


def test_no_supervisord_program_runs_under_uv_run() -> None:
    offenders = _programs_under_uv_run(_SUPERVISORD_DIR)
    assert not offenders, (
        "These supervisord programs run under `uv run`, which stays resident as their parent and can "
        "relock the workspace. Use `bash -c \"uv sync --all-packages --frozen && exec .venv/bin/<name>\"` "
        "(see .agents/shared/references/running-python.md):\n" + "\n".join(offenders)
    )


def test_a_planted_uv_run_program_is_caught_in_either_form(tmp_path: Path) -> None:
    dropins = tmp_path / "supervisord.conf.d"
    dropins.mkdir()
    (tmp_path / "supervisord.conf").write_text("[supervisord]\nnodaemon=true\n")
    (dropins / "direct.conf").write_text("[program:direct]\ncommand=uv run direct\n")
    (dropins / "tagged.conf").write_text(
        "[program:tagged]\ncommand=python3 system/services/oom_priority/bin/oom_tag_service.py "
        'user bash -c "python3 system/scripts/forward_port.py --name t --url http://x && exec uv run tagged"\n'
    )
    (dropins / "fine.conf").write_text(
        '[program:fine]\ncommand=bash -c "uv sync --all-packages --frozen && exec .venv/bin/fine"\n'
    )

    offenders = _programs_under_uv_run(tmp_path)

    assert [offender.split(" ", 1)[0] for offender in offenders] == ["direct.conf", "tagged.conf"]


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
        if not any(relative == path and context in line for relative, line in violations)
    ]
    assert not stale, f"These deliberate-sync exceptions no longer match any line: {stale}"


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


def test_changelogs_and_fixtures_are_not_scanned() -> None:
    scanned = {str(path.relative_to(_REPO_ROOT)) for path in _agent_facing_files()}

    assert not any("/changelog/" in path or path.endswith("CHANGELOG.md") for path in scanned)
    assert "AGENTS.md" in scanned


# ``python3 [flags] <path>.py``, the path optionally quoted (JSON escapes its quotes).
_PYTHON3_SCRIPT_RE = re.compile(r"(?<![\w./-])python3((?:\s+-[A-Za-z]+)*)\s+\\?[\"']?([^\s\"'`)\\]+\.py)\b")
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


def _named_python3_scripts(text: str, file_dir: Path, repo_root: Path) -> list[tuple[str, Path | None]]:
    """Every ``(as written, resolved path)`` a ``python3`` in ``text`` runs; None when it cannot be resolved.

    A placeholder (``<name>``) or a scratch path under /tmp names no file in the tree and is skipped.
    """
    found: list[tuple[str, Path | None]] = []
    for line in text.splitlines():
        for match in _PYTHON3_SCRIPT_RE.finditer(line):
            if _UNDER_UV_RUN_RE.search(line[: match.start()]):
                continue
            written = match.group(2)
            if "<" in written or written.startswith("/tmp/"):
                continue
            path = written
            base = repo_root
            for prefix in _REPO_ROOT_PREFIXES:
                if path.startswith(prefix):
                    path = path.removeprefix(prefix)
                    break
            else:
                if path.startswith("$script_dir/"):
                    path, base = path.removeprefix("$script_dir/"), file_dir
            found.append((written, None if "$" in path else (base / path).resolve()))
    return found


def _python3_violations(files: list[Path], repo_root: Path) -> list[str]:
    return [
        f"{path.relative_to(repo_root)}: python3 {written}"
        for path in files
        for written, resolved in _named_python3_scripts(path.read_text(errors="replace"), path.parent, repo_root)
        if resolved is None or not is_bare_entry(resolved)
    ]


def test_python3_invocations_name_bare_entry_points() -> None:
    offenders = _python3_violations(_agent_facing_files(_PYTHON3_SCANNED_SUFFIXES), _REPO_ROOT)
    assert not offenders, (
        "These run a script with the system python3, but it is not a bare entry point (or does not exist): "
        "the system python3 has no venv. Run it with `uv run --no-sync <path>` instead "
        "(see .agents/shared/references/running-python.md):\n" + "\n".join(offenders)
    )


def test_the_python3_scan_catches_a_venv_script_and_a_stale_path(tmp_path: Path) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text(
        "\n".join(
            [
                "python3 .agents/skills/notify-user/scripts/notify_user.py 'done'",
                "python3 system/scripts/forward_port.py --name x --url http://localhost:1",
                "python3 system/scripts/no_such_script.py",
                "uv run --no-sync python3 .agents/skills/notify-user/scripts/notify_user.py 'fine'",
                'command": "python3 \\"$MNGR_AGENT_WORK_DIR/system/scripts/agent_rewrite_bash_command.py\\""',
            ]
        )
    )

    offenders = [
        written
        for written, resolved in _named_python3_scripts(doc.read_text(), doc.parent, _REPO_ROOT)
        if resolved is None or not is_bare_entry(resolved)
    ]

    assert offenders == [
        ".agents/skills/notify-user/scripts/notify_user.py",
        "system/scripts/no_such_script.py",
    ]


# Path edits are how an entry file that runs with no venv reaches code outside its own
# directory -- a stdlib-only library, or its skill's python/ package -- and update-self's way
# of reaching the oom_priority of the tree it applies.
_ALLOWED_SYS_PATH_EDITS = frozenset(
    {
        "system/scripts/agent_block_pipe_tail_head_check.py",
        "system/scripts/agent_latchkey_request_check.py",
        "system/scripts/agent_rewrite_bash_command.py",
        "system/scripts/agent_secrets_guard_check.py",
        "system/scripts/agent_tk_standalone_check.py",
        # Skill entry files that run with no workspace venv, reaching the package in python/.
        ".agents/skills/update-self/scripts/update_self.py",
        ".agents/skills/update-self/scripts/run_in_background.py",
        ".agents/skills/publish-template/scripts/validate_template.py",
        ".agents/skills/publish-template/scripts/write_template_manifest.py",
        ".agents/skills/update-self/python/update_self_skill/update_banding.py",
    }
)


def _edits_sys_path(source: str) -> bool:
    for node in ast.walk(ast.parse(source)):
        target = node.func.value if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) else None
        if isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            target = next((t.value if isinstance(t, ast.Subscript) else t for t in targets), None)
        if isinstance(target, ast.Attribute) and target.attr == "path" and isinstance(target.value, ast.Name):
            if target.value.id == "sys":
                return True
    return False


def _sys_path_editors(repo_root: Path) -> list[str]:
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.py"], cwd=repo_root, capture_output=True, text=True, check=True
    ).stdout.split("\0")
    return sorted(
        relative
        for relative in tracked
        if relative
        and "vendor" not in PurePosixPath(relative).parts
        and not is_test_file(Path(relative))
        and not (repo_root / relative).parent in STANDALONE_BARE_DIRS
        and relative not in _ALLOWED_SYS_PATH_EDITS
        and (repo_root / relative).is_file()
        and _edits_sys_path((repo_root / relative).read_text())
    )


def test_only_the_declared_bare_stubs_edit_sys_path() -> None:
    offenders = _sys_path_editors(_REPO_ROOT)
    assert not offenders, (
        "These edit sys.path. Import the package instead: a skill's modules live in its python/ "
        "project and a system script's beside it, all importable from the root venv:\n"
        + "\n".join(offenders)
    )


def test_the_sys_path_scan_sees_each_form_of_edit() -> None:
    assert _edits_sys_path("import sys\nsys.path.insert(0, 'x')\n")
    assert _edits_sys_path("import sys\nsys.path.append('x')\n")
    assert _edits_sys_path("import sys\nsys.path[:0] = ['x']\n")
    assert _edits_sys_path("import sys\nsys.path += ['x']\n")
    assert not _edits_sys_path("import sys\nprint(sys.path)\n")
