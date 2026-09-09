"""Contract tests between the skill prose that dispatches workers and the code
that carries the dispatch.

Run via: ``uv run pytest .agents/skills/launch-task/scripts/dispatch_contract_test.py``

A worker dispatch crosses three hands: a lead skill's prose writes a task file
and invokes ``create_worker.py``; ``create_worker.py`` provisions the worker and
polls for the report named in that task file; the worker reads the task file
back with ``parse_task_frontmatter.py`` and pushes its report to the path it
names. Each hand has its own unit tests; nothing else checks that they agree.
These tests take the prose literally -- they execute the real fenced ``bash``
blocks that write task files and parse the real ``create_worker.py`` argvs the
prose contains -- so a drift in any hand (a renamed flag, a moved runtime dir,
a task file the worker's glob no longer finds) fails here instead of in a live
worker.

The dispatching skills are discovered, not listed: any markdown under
``.agents/skills`` or ``.agents/shared`` whose fenced code both writes a
``task.md`` by heredoc and invokes ``create_worker.py launch``.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent
# This file lives at .agents/skills/launch-task/scripts/<file>; the repo root
# is four directories up.
_REPO_ROOT = _SCRIPTS_DIR.parents[3]
_SHARED_SCRIPTS_DIR = _REPO_ROOT / ".agents" / "shared" / "scripts"
_PROSE_ROOTS = (_REPO_ROOT / ".agents" / "skills", _REPO_ROOT / ".agents" / "shared")
_HARDEN_WORKER_SKILL = _REPO_ROOT / ".agents" / "shared" / "worker" / "SKILL.md"


def _load_script_module(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


create_worker = _load_script_module(_SCRIPTS_DIR / "create_worker.py")
parse_task_frontmatter = _load_script_module(
    _SHARED_SCRIPTS_DIR / "parse_task_frontmatter.py"
)

# A fenced block opened by three or more backticks and closed by the same
# number, so a block that nests a ``` fence (opened with ````) is taken whole.
_FENCED_CODE = re.compile(r"^(`{3,})[^\n]*\n(.*?)^\1[ \t]*$", re.DOTALL | re.MULTILINE)
# The worker-side glob a task body hands its worker, per worker-reporting.md.
_TASK_FILE_GLOB_SUBSTITUTION = re.compile(r"`<TASK_FILE_GLOB>`\s*->\s*`([^`]+)`")
# The glob the installed harden-worker skill parses its task file from.
_PARSE_INVOCATION_GLOB = re.compile(r"parse_task_frontmatter\.py\s+'([^']+)'")

# Placeholder values for the slots the prose leaves to the agent. Shell
# variables are supplied through the environment the block runs in; the
# angle-bracket slug (used at the shell level as a path component) is
# substituted textually, since bash would read ``<slug>`` as a redirection.
_PLACEHOLDER_ENV = {
    "NAME": "demo",
    "TARGET": "demo",
    "REF": "demo-ref",
}
_TEXT_PLACEHOLDERS = {"<slug>": "demo"}
_LEAD_NAME = "lead-demo"


@dataclass
class _RecordedCall:
    argv: list[str]
    kwargs: dict[str, Any]


@dataclass
class _CleanResult:
    """``subprocess.CompletedProcess`` stand-in: success with empty output, which
    launch's ``git status --porcelain`` preflight reads as a clean tree."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class _RecordingRunner(create_worker.Runner):
    """Records every command launch would run instead of spawning ``mngr``."""

    calls: list[_RecordedCall] = field(default_factory=list)

    def run(self, argv: Sequence[str], **kwargs):
        self.calls.append(_RecordedCall(argv=list(argv), kwargs=kwargs))
        return _CleanResult()


def _fenced_blocks(text: str) -> list[str]:
    return [match.group(2) for match in _FENCED_CODE.finditer(text)]


def _prose_files() -> list[Path]:
    return sorted(p for root in _PROSE_ROOTS for p in root.rglob("*.md"))


def _launcher_invocations(block: str) -> list[list[str]]:
    """Every ``create_worker.py <argv...>`` command in a fenced block, as the
    argv after the script path (line continuations joined, comments dropped)."""
    invocations: list[list[str]] = []
    for command in re.split(r"\n(?=\S)", block.replace("\\\n", " ")):
        if "create_worker.py" not in command:
            continue
        words = shlex.split(command, comments=True)
        for index, word in enumerate(words):
            if word.endswith("create_worker.py"):
                invocations.append(words[index + 1 :])
                break
    return invocations


def _substitute(text: str) -> str:
    for placeholder, value in _TEXT_PLACEHOLDERS.items():
        text = text.replace(placeholder, value)
    for name, value in _PLACEHOLDER_ENV.items():
        text = text.replace(f"${name}", value)
    return text


@dataclass(frozen=True)
class _Dispatcher:
    """One skill that writes a task file and launches a worker for it."""

    prose: Path
    task_block: str
    launch_argv: list[str]

    @property
    def name(self) -> str:
        return self.prose.parent.name

    def launch_option(self, flag: str) -> str:
        return _substitute(self.launch_argv[self.launch_argv.index(flag) + 1])


def _dispatchers() -> list[_Dispatcher]:
    found: list[_Dispatcher] = []
    for prose in _prose_files():
        blocks = _fenced_blocks(prose.read_text(encoding="utf-8"))
        task_blocks = [b for b in blocks if "task.md" in b and "<<" in b]
        launches = [
            argv
            for block in blocks
            for argv in _launcher_invocations(block)
            if argv[:1] == ["launch"]
        ]
        if not task_blocks or not launches:
            continue
        assert len(task_blocks) == 1, (
            f"{prose}: expected one task-file block, found {len(task_blocks)}"
        )
        assert len(launches) == 1, (
            f"{prose}: expected one launch invocation, found {len(launches)}"
        )
        found.append(
            _Dispatcher(prose=prose, task_block=task_blocks[0], launch_argv=launches[0])
        )
    return found


_DISPATCHERS = _dispatchers()


def test_discovery_finds_the_generic_and_harden_dispatchers() -> None:
    """Vacuity guard for the parametrized tests below: the skills this contract
    exists for must be among the discovered dispatchers."""
    names = {d.name for d in _DISPATCHERS}
    assert names >= {
        "launch-task",
        "crystallize-creation",
        "update-creation",
        "heal-creation",
    }, names


def _all_prose_launcher_invocations() -> list[tuple[str, list[str]]]:
    return [
        (str(prose.relative_to(_REPO_ROOT)), argv)
        for prose in _prose_files()
        for block in _fenced_blocks(prose.read_text(encoding="utf-8"))
        for argv in _launcher_invocations(block)
    ]


_PROSE_INVOCATIONS = _all_prose_launcher_invocations()


def test_prose_invokes_launcher_subcommands_that_exist() -> None:
    assert {argv[0] for _, argv in _PROSE_INVOCATIONS} >= {
        "launch",
        "await",
        "launch-sync",
        "destroy",
    }


@pytest.mark.parametrize(
    "prose,argv", _PROSE_INVOCATIONS, ids=[f"{p}:{a[0]}" for p, a in _PROSE_INVOCATIONS]
)
def test_prose_launcher_invocation_is_accepted_by_the_real_parser(
    prose: str, argv: list[str]
) -> None:
    """Every ``create_worker.py`` command an agent would copy out of the prose
    parses against the script's real argparse surface: the subcommand exists,
    every flag is known, required flags are present, and typed values (a
    duration) are well-formed."""
    substituted = [_substitute(word) for word in argv]
    try:
        create_worker.build_parser().parse_args(substituted)
    except SystemExit as exc:
        raise AssertionError(
            f"{prose}: `create_worker.py {' '.join(argv)}` is rejected by the "
            f"script's own parser (exit {exc.code})"
        ) from exc


def _run_task_block(dispatcher: _Dispatcher, cwd: Path) -> Path:
    """Execute the dispatcher's task-file block in ``cwd`` (with the runtime dir
    its launch names already present, as the skill's earlier step creates it)
    and return the task file the launch invocation points at."""
    runtime_dir = cwd / dispatcher.launch_option("--runtime-dir")
    runtime_dir.mkdir(parents=True, exist_ok=True)
    env = {
        **_PLACEHOLDER_ENV,
        "MNGR_AGENT_NAME": _LEAD_NAME,
        "PATH": os.environ["PATH"],
    }
    block = _substitute(dispatcher.task_block)
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", block],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"{dispatcher.prose}: task-file block failed under bash:\n{result.stderr}"
    )
    task_file = cwd / dispatcher.launch_option("--task-file")
    assert task_file.is_file(), (
        f"{dispatcher.prose}: the task-file block did not write the file its "
        f"launch invocation passes as --task-file ({task_file.relative_to(cwd)})"
    )
    return task_file


def _worker_glob_for(task_text: str, frontmatter: dict[str, str]) -> str:
    """The glob the worker will parse this task file from.

    A task body may hand its worker the glob directly (the
    ``<TASK_FILE_GLOB>`` substitution from worker-reporting.md); otherwise a
    harden flow (frontmatter ``operation``) relies on the glob baked into the
    installed harden-worker skill.
    """
    handed = _TASK_FILE_GLOB_SUBSTITUTION.search(task_text)
    if handed is not None:
        return handed.group(1)
    assert "operation" in frontmatter, (
        "task body names no `<TASK_FILE_GLOB>` and is not a harden flow -- the "
        "worker has no way to know where its task file is"
    )
    baked = _PARSE_INVOCATION_GLOB.search(_HARDEN_WORKER_SKILL.read_text())
    assert baked is not None, f"{_HARDEN_WORKER_SKILL}: no parse invocation"
    return baked.group(1)


@pytest.mark.parametrize("dispatcher", _DISPATCHERS, ids=[d.name for d in _DISPATCHERS])
def test_task_block_writes_a_file_the_worker_parses_and_finds(
    dispatcher: _Dispatcher, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The skill's own task-file block, run verbatim, yields a task file that
    the worker-side parser accepts, whose report path sits inside the runtime
    dir the launch syncs, and that the worker's glob resolves to."""
    monkeypatch.chdir(tmp_path)
    task_file = _run_task_block(dispatcher, tmp_path)
    runtime_dir = Path(dispatcher.launch_option("--runtime-dir"))

    frontmatter = parse_task_frontmatter.parse(task_file)

    report_path = Path(frontmatter["finish_report_path"])
    # The runtime dir is what launch rsyncs into the worker, and the worker
    # pushes its report to dirname(finish_report_path) on the lead: both only
    # line up when the report path lives inside the runtime dir.
    assert report_path.is_relative_to(runtime_dir), (
        f"finish_report_path {report_path} is outside runtime dir {runtime_dir}"
    )
    assert report_path.name == "report.md"
    if dispatcher.name in {"crystallize-creation", "update-creation", "heal-creation"}:
        # The harden-worker skill dispatches on these two fields.
        assert frontmatter["operation"] == dispatcher.name.removesuffix("-creation")
        assert frontmatter["type"]
    resolved = parse_task_frontmatter.resolve(
        _worker_glob_for(task_file.read_text(), frontmatter)
    )
    assert resolved.resolve() == task_file.resolve()


@pytest.mark.parametrize("dispatcher", _DISPATCHERS, ids=[d.name for d in _DISPATCHERS])
def test_launch_on_the_real_task_file_syncs_runtime_dir_and_addresses_the_worker(
    dispatcher: _Dispatcher, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running the launch the prose specifies, on the task file the prose
    writes: the lead's poll target is the worker's report path, the runtime
    dir holding it is what gets synced, the task file is what gets messaged,
    and the launcher stamps the lead's address so the worker's parse sees it."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MNGR_AGENT_NAME", _LEAD_NAME)
    task_file = _run_task_block(dispatcher, tmp_path)
    runner = _RecordingRunner()
    launch_argv = [_substitute(word) for word in dispatcher.launch_argv]

    rc = create_worker.main(launch_argv, runner=runner)

    assert rc == 0
    argvs = [call.argv for call in runner.calls]
    worker_name = dispatcher.launch_option("--name")
    runtime_dir = create_worker._normalize_dir(
        dispatcher.launch_option("--runtime-dir")
    )
    assert [
        "mngr",
        "rsync",
        f"./{runtime_dir}",
        f"{worker_name}:{runtime_dir}",
        "--uncommitted-changes=merge",
    ] in argvs
    assert argvs[-1] == [
        "mngr",
        "message",
        worker_name,
        "--message-file",
        str(task_file.relative_to(tmp_path)),
    ]
    # Lead and worker agree on the report file: what await polls for is what
    # the worker's parse hands back as FINISH_REPORT_PATH.
    frontmatter = parse_task_frontmatter.parse(task_file)
    assert create_worker._read_finish_report_path(task_file) == Path(
        frontmatter["finish_report_path"]
    )
    assert frontmatter["lead_agent"] == _LEAD_NAME
