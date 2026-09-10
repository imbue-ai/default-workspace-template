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
a task file the worker cannot parse at the path it was handed) fails here
instead of in a live worker.

The dispatching skills are discovered, not listed: any ``SKILL.md`` under
``.agents/skills`` whose fenced code both writes a ``task.md`` by heredoc and
invokes ``create_worker.py launch``. Only skills dispatch -- a reference under
``references/`` documents the moves, and the shared material under
``.agents/shared`` is read by workers -- so a task-file block found anywhere
else is prose about dispatch, not a dispatcher. Every ``create_worker.py``
invocation in *all* of that prose is still checked against the real parser
below; it is only the run-the-block-and-launch pair that needs a real
dispatcher.
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
_SKILLS_ROOT = _REPO_ROOT / ".agents" / "skills"
_PROSE_ROOTS = (_SKILLS_ROOT, _REPO_ROOT / ".agents" / "shared")


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

# Placeholder values for the slots the prose leaves to the agent. Shell
# variables are supplied through the environment the block runs in; the
# angle-bracket slug (used at the shell level as a path component) is
# substituted textually, since bash would read ``<slug>`` as a redirection.
_PLACEHOLDER_ENV = {
    "NAME": "demo",
    "TARGET": "demo",
    "REF": "demo-ref",
}
# The worker-side ``report`` invocation is written against values the worker
# only holds at runtime -- the path it was handed, the kind of report it decided
# to send -- so the prose spells them as angle-bracket slots or as the shell
# variables ``parse_task_frontmatter.py`` exports. Both forms get a concrete
# stand-in here so the argv still reaches the real parser with a real value in
# every slot; without them a typed or required flag would be checked against a
# literal ``<...>``.
_TEXT_PLACEHOLDERS = {
    "<slug>": "demo",
    "<TASK_FILE>": "data/.tasks/launch-task/demo/task.md",
    "$TASK_FILE": "data/.tasks/launch-task/demo/task.md",
    "<REPORT_TYPE>": "status",
    "<NAME>": "done",
    "<BODY_FILE>": "data/.tasks/launch-task/demo/body.md",
    "$FINISH_REPORT_PATH": "data/.tasks/launch-task/demo/reports/report.md",
}
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
    """Records every command launch would run instead of spawning ``mngr``.

    The two real git probes launch makes before it creates anything are
    answered here: ``git status --porcelain`` reads as a clean tree, and ``git
    rev-parse --show-toplevel`` names ``repo_root`` -- the temporary directory
    each test runs the prose in, standing in for the lead's checkout.
    """

    repo_root: Path
    calls: list[_RecordedCall] = field(default_factory=list)

    def run(self, argv: Sequence[str], **kwargs):
        self.calls.append(_RecordedCall(argv=list(argv), kwargs=kwargs))
        if list(argv)[:2] == ["git", "rev-parse"]:
            return _CleanResult(stdout=f"{self.repo_root}\n")
        return _CleanResult()


def _fenced_blocks(text: str) -> list[str]:
    return [match.group(2) for match in _FENCED_CODE.finditer(text)]


def _prose_files() -> list[Path]:
    """Every markdown file that may contain a ``create_worker.py`` invocation."""
    return sorted(p for root in _PROSE_ROOTS for p in root.rglob("*.md"))


def _dispatcher_candidates() -> list[Path]:
    """The files a dispatcher can be found in: skill entry points.

    A skill's own ``SKILL.md`` is the only place a dispatch is actually
    performed. Its ``references/`` describe the moves and the shared worker
    material under ``.agents/shared`` is read from the far side of the
    dispatch, so scanning either would find prose *about* task files and try to
    run it as a dispatcher.
    """
    return sorted(_SKILLS_ROOT.glob("*/SKILL.md"))


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
    for prose in _dispatcher_candidates():
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


@pytest.mark.parametrize("dispatcher", _DISPATCHERS, ids=[d.name for d in _DISPATCHERS])
def test_task_block_writes_a_file_the_worker_parses_at_the_path_launch_names(
    dispatcher: _Dispatcher, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The skill's own task-file block, run verbatim, yields a task file that
    the worker-side parser accepts *at the exact path the launch passes*, and
    whose report path sits inside the runtime dir the launch syncs.

    The exact path is the whole contract on the worker's side: it is handed
    that path in its task message and parses it directly, so a block that
    writes its file anywhere other than where the launch points is a dispatch
    that cannot be read on arrival.
    """
    monkeypatch.chdir(tmp_path)
    _run_task_block(dispatcher, tmp_path)
    runtime_dir = Path(dispatcher.launch_option("--runtime-dir"))
    # Exactly the string the launch passes as --task-file, parsed as-is: no
    # resolving, no globbing, no absolute path built by the test.
    named_path = Path(dispatcher.launch_option("--task-file"))

    frontmatter = parse_task_frontmatter.parse(named_path)

    report_path = Path(frontmatter["finish_report_path"])
    # The runtime dir is what launch rsyncs into the worker, and the worker
    # pushes its report to dirname(finish_report_path) on the lead: both only
    # line up when the report path lives inside the runtime dir.
    assert report_path.is_relative_to(runtime_dir), (
        f"finish_report_path {report_path} is outside runtime dir {runtime_dir}"
    )
    assert report_path.name == "report.md"
    if dispatcher.name in {"crystallize-creation", "update-creation", "heal-creation"}:
        # The generic worker dispatches on these two fields.
        assert frontmatter["operation"] == dispatcher.name.removesuffix("-creation")
        assert frontmatter["type"]


@pytest.mark.parametrize("dispatcher", _DISPATCHERS, ids=[d.name for d in _DISPATCHERS])
def test_launch_on_the_real_task_file_syncs_runtime_dir_and_addresses_the_worker(
    dispatcher: _Dispatcher, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running the launch the prose specifies, on the task file the prose
    writes: the lead's poll target is the worker's report path, the runtime
    dir holding it is what gets synced, the task file is what gets messaged,
    and the launcher stamps (and labels) the lead's address and stamps the task
    file's own path, so the worker's parse sees both."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MNGR_AGENT_NAME", _LEAD_NAME)
    task_file = _run_task_block(dispatcher, tmp_path)
    runner = _RecordingRunner(repo_root=tmp_path)
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
        "--uncommitted-changes=clobber",
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
    # Both stamps the worker depends on landed, and both name what the launch
    # itself used: the lead that will poll for the report, and the very path
    # the task file was messaged from (repo-relative -- tmp_path stands in for
    # the lead's checkout root).
    assert frontmatter["lead_agent"] == _LEAD_NAME
    assert frontmatter["task_file"] == dispatcher.launch_option("--task-file")
    create_argv = next(argv for argv in argvs if argv[:2] == ["mngr", "create"])
    assert create_argv[-2:] == ["--label", f"lead_agent={_LEAD_NAME}"]
