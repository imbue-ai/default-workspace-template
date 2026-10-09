"""How Python gets launched in this workspace (see .agents/shared/references/running-python.md).

Two rules, each checked against the real tree and against a planted violation:

- A long-running program never runs under ``uv run``: supervisord programs (built-in
  and user-added drop-ins alike) sync ``--frozen`` and then ``exec .venv/bin/<name>``,
  so no ``uv`` process stays resident as their parent.
- A one-off written into an agent-facing file runs ``uv run --no-sync``, so following
  an instruction can never relock or sync the workspace behind the agent's back.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path, PurePosixPath

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SUPERVISORD_DIR = _REPO_ROOT / "system"

_SHELL_PROGRAMS = frozenset({"bash", "sh", "zsh", "dash"})

# The file types agents read commands out of (docs, prompts, config, scripts).
_AGENT_FACING_SUFFIXES = (".md", ".toml", ".sh", ".conf")
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

# Deliberate syncing ``uv run`` uses: (repo-relative path, a substring of the line).
_DELIBERATE_SYNCS: tuple[tuple[str, str], ...] = (
    # The update worker reads footprints and test selections with the app-manifest
    # library as the merge left it, so it syncs that package from the merged lock first.
    (
        ".agents/skills/update-self/references/update-self-worker.md",
        "uv run --frozen --package app-manifest app-manifest",
    ),
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


def _agent_facing_files() -> list[Path]:
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], cwd=_REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.split("\0")
    return [
        _REPO_ROOT / relative
        for relative in tracked
        if relative.endswith(_AGENT_FACING_SUFFIXES)
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
