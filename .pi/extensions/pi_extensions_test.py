"""Behavioural tests for this workspace's pi extensions.

pi has no shell-hook surface, so the rules claude and codex get from
``system/scripts/`` reach a pi agent as the two TypeScript extensions here:
``policy_guards.ts`` (the command guards and rewrite) and ``tk_workflow.ts``
(the step discipline). They run inside pi's Node process, so they are exercised the way
mngr exercises its own pi extension -- drive the real file with a synthetic
event through Node and assert on what the handler returns -- rather than
reimplemented in Python. Skipped automatically when Node (with TypeScript
support) is unavailable; the ``.ts`` files are resources, not Python, so they do
not count toward coverage.

``policy_guards.ts`` resolves its scripts from ``MNGR_AGENT_WORK_DIR``, and the
scripts live in this repo, so those tests point it at the repo root and run the
real PreToolUse hook scripts and ``agent_rewrite_bash_command.py`` -- covering
the extension and its wiring together. ``tk_workflow.ts`` reads step state
from the vendored ``ticket`` script, so those tests point it at a temp tree with
a stub ``ticket`` whose output a test can drive.

See ``system/apps/chat/imbue/chat/harnesses/core-contracts/tool-call-policies.md`` for what each rule enforces on each
harness.
"""

from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

# Every test here starts a Node process that type-strips a `.ts` module, and most of
# them have it spawn a guard script or a bash `ticket` in turn. That runs in well
# under a second warm, but the suite's global 10s per-test timeout budgets for pure
# Python; a cold cache or a loaded machine pushes these past it. Give them room.
_NODE_EVENT_TIMEOUT_SECONDS = 60
pytestmark = pytest.mark.timeout(_NODE_EVENT_TIMEOUT_SECONDS)
# Kept under the above so a genuinely hung Node fails as a TimeoutExpired naming the
# command, rather than as pytest's opaque per-test timeout.
_NODE_SUBPROCESS_TIMEOUT_SECONDS = 30

_EXTENSIONS_DIR = Path(__file__).parent
_REPO_ROOT = _EXTENSIONS_DIR.parents[1]
_POLICY_GUARDS = _EXTENSIONS_DIR / "policy_guards.ts"
_TK_WORKFLOW = _EXTENSIONS_DIR / "tk_workflow.ts"

# Node driver: load one extension by absolute path, register its handlers against a
# fake `pi`, fire a single event, and report the handler's return value and the event
# payload (which a handler may mutate) as JSON.
_DRIVER_MJS = """
import { pathToFileURL } from "node:url";
const [, , extensionPath, specJson] = process.argv;
const spec = JSON.parse(specJson);
const handlers = {};
const mod = await import(pathToFileURL(extensionPath).href);
mod.default({ on: (name, handler) => { (handlers[name] ||= []).push(handler); } });
let result;
const payload = spec.payload ?? {};
for (const handler of (handlers[spec.event] || [])) { result = await handler(payload, {}); }
process.stdout.write(JSON.stringify({ result: result ?? null, payload }));
"""

# Stub `ticket`: its `steps` output is driven by env so a test can model any step
# state. Exits non-zero with no output (like the real tk) when the requested set is
# empty -- the extension reads that as "consulted, no steps". With
# STUB_ECHO_TICKETS_DIR set it instead reports the TICKETS_DIR the child process
# actually received, so a test can pin what the extension exports to it.
_STUB_TICKET = """#!/usr/bin/env bash
if [[ "$1" == "steps" && -n "${STUB_ECHO_TICKETS_DIR:-}" ]]; then
  printf 'tickets_dir=%s' "${TICKETS_DIR:-unset}"
  exit 0
fi
if [[ "$1" == "steps" && "$2" == "--status=in_progress" ]]; then
  printf '%s' "${STUB_INPROGRESS:-}"
  [[ -n "${STUB_INPROGRESS:-}" ]] && exit 0 || exit 2
fi
if [[ "$1" == "steps" ]]; then
  printf '%s' "${STUB_STEPS:-}"
  [[ -n "${STUB_STEPS:-}" ]] && exit 0 || exit 2
fi
exit 0
"""

_HOST = "http://latchkey-self.invalid/permission-requests"
# The canonical filing, exactly as the latchkey skill documents it.
_REQUEST = f"latchkey curl -XPOST {_HOST} -H 'Content-Type: application/json' -d '{{\"agent_id\": \"a1\"}}'"


@functools.cache
def _node_that_imports_typescript() -> str | None:
    """The Node binary, if one is installed and can import a `.ts` module (strip-types,
    Node >= ~22.6); otherwise None. Probed once per session -- every test below spawns
    Node, and re-probing per test doubled the file's runtime."""
    node = shutil.which("node")
    if node is None:
        return None
    with tempfile.TemporaryDirectory() as probe_dir:
        (Path(probe_dir) / "probe.ts").write_text("export const value: number = 1;\n")
        probe_mjs = Path(probe_dir) / "probe.mjs"
        probe_mjs.write_text(
            "const m = await import('./probe.ts'); process.exit(m.value === 1 ? 0 : 1);\n"
        )
        result = subprocess.run(
            [node, str(probe_mjs)],
            capture_output=True,
            text=True,
            timeout=_NODE_SUBPROCESS_TIMEOUT_SECONDS,
        )
    return node if result.returncode == 0 else None


def _run_event(
    tmp_path: Path,
    extension: Path,
    event: str,
    payload: dict[str, Any],
    *,
    work_dir: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Fire one ``event`` through ``extension``, with ``work_dir`` as the agent's work dir.

    Returns the completed process: parse ``.stdout`` for ``{"result": ..., "payload": ...}``
    (see ``_event_output``) and read ``.stderr`` for anything a handler wrote there.
    """
    node = _node_that_imports_typescript()
    if node is None:
        pytest.skip("node with TypeScript module support is not available")
    driver = tmp_path / "driver.mjs"
    driver.write_text(_DRIVER_MJS)
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    full_env = {
        "PATH": os.environ.get("PATH", ""),
        "MNGR_AGENT_WORK_DIR": str(work_dir),
        "MNGR_AGENT_STATE_DIR": str(state_dir),
    }
    full_env.update(env or {})
    return subprocess.run(
        [
            node,
            str(driver),
            str(extension),
            json.dumps({"event": event, "payload": payload}),
        ],
        capture_output=True,
        text=True,
        timeout=_NODE_SUBPROCESS_TIMEOUT_SECONDS,
        env=full_env,
    )


def _event_output(proc: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """The driver's ``{"result": ..., "payload": ...}`` report."""
    assert proc.returncode == 0, f"event driver failed:\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout)


def _event_result(proc: subprocess.CompletedProcess[str]) -> Any:
    return _event_output(proc)["result"]


def _guard_call(
    tmp_path: Path, command: str, env: dict[str, str] | None = None
) -> tuple[Any, str]:
    """Fire a bash ``tool_call`` through policy_guards.ts against the real scripts.

    Returns the handler's result and the command pi would run afterwards.
    """
    payload = {"toolName": "bash", "input": {"command": command}}
    proc = _run_event(
        tmp_path, _POLICY_GUARDS, "tool_call", payload, work_dir=_REPO_ROOT, env=env
    )
    out = _event_output(proc)
    return out["result"], out["payload"]["input"]["command"]


def _guard_result(tmp_path: Path, command: str) -> Any:
    return _guard_call(tmp_path, command)[0]


def _tk_work_dir(tmp_path: Path) -> Path:
    """A work dir holding the stub ``ticket`` and a tickets dir for tk_workflow.ts."""
    work_dir = tmp_path / "work"
    tk_dir = work_dir / "system" / "vendor" / "tk"
    tk_dir.mkdir(parents=True, exist_ok=True)
    ticket = tk_dir / "ticket"
    ticket.write_text(_STUB_TICKET)
    ticket.chmod(0o755)
    (work_dir / ".tickets").mkdir(exist_ok=True)
    return work_dir


def _tk_result(
    tmp_path: Path,
    event: str,
    payload: dict[str, Any],
    *,
    in_progress: str = "",
    steps: str = "",
) -> Any:
    """Fire ``event`` through tk_workflow.ts with the stub tk reporting this step state."""
    work_dir = _tk_work_dir(tmp_path)
    return _event_result(
        _run_event(
            tmp_path,
            _TK_WORKFLOW,
            event,
            payload,
            work_dir=work_dir,
            env={"STUB_INPROGRESS": in_progress, "STUB_STEPS": steps},
        )
    )


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(_REQUEST, id="lone-permission-request"),
        pytest.param("tk start wor-1", id="standalone-tk-start"),
        pytest.param("cd /tmp && echo hi", id="chained-command-no-checker-cares-about"),
        pytest.param(f"latchkey curl {_HOST} | jq .", id="reading-the-queue"),
        pytest.param("cat notes.md | " + "head -120", id="cat-of-files-into-head"),
        pytest.param("git commit -m 'rebase notes'", id="plain-git-commit"),
    ],
)
def test_guards_allow_what_the_checkers_allow(tmp_path: Path, command: str) -> None:
    assert _guard_result(tmp_path, command) is None


@pytest.mark.parametrize(
    ("command", "expected_in_reason"),
    [
        pytest.param(
            f"{_REQUEST} && {_REQUEST}",
            "more than one permission request",
            id="batched-requests",
        ),
        pytest.param(
            f"{_REQUEST} > /tmp/out.json",
            "output is redirected",
            id="redirected-request",
        ),
        pytest.param(
            "cd /tmp && tk start wor-1", "runs before it", id="chained-tk-start"
        ),
        pytest.param(
            "pytest | " + "tail -20",
            "Do not pipe commands through tail or head",
            id="pipe-into-tail",
        ),
        pytest.param(
            "git rebase -i HEAD~2",
            "git rebase commands are not allowed",
            id="git-rebase",
        ),
        pytest.param(
            "git commit --amend -m x",
            "--amend or --fixup is not allowed",
            id="git-commit-amend",
        ),
    ],
)
def test_guards_block_with_the_checkers_own_reason(
    tmp_path: Path, command: str, expected_in_reason: str
) -> None:
    """The refusal carries the script's stderr, so the agent reads the same guidance
    it would get from the PreToolUse hook on claude or codex."""
    result = _guard_result(tmp_path, command)
    assert result is not None and result["block"] is True
    assert expected_in_reason in result["reason"]


def test_guards_ignore_a_non_bash_tool(tmp_path: Path) -> None:
    payload = {"toolName": "read", "input": {"command": f"{_REQUEST} && {_REQUEST}"}}
    assert (
        _event_result(
            _run_event(
                tmp_path, _POLICY_GUARDS, "tool_call", payload, work_dir=_REPO_ROOT
            )
        )
        is None
    )


def test_an_allowed_command_runs_with_the_oom_tag_and_git_identity_prefix(
    tmp_path: Path,
) -> None:
    """The guards judge the command the agent wrote -- a prefixed `tk start` would read
    as chained and be refused -- and only then is the prefix put in front of it."""
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    (host_dir / "data.json").write_text(json.dumps({"host_id": "host-9"}))
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "data.json").write_text(json.dumps({"name": "test-agent"}))
    env = {
        "MNGR_AGENT_ID": "agent-x",
        "MNGR_HOST_DIR": str(host_dir),
        "MNGR_AGENT_STATE_DIR": str(state_dir),
    }
    result, command = _guard_call(tmp_path, "tk start wor-1", env=env)
    assert result is None
    prefix = command.removesuffix("tk start wor-1")
    assert prefix != command
    assert "GIT_AUTHOR_NAME=test-agent" in prefix
    assert "GIT_AUTHOR_EMAIL=agent-x@host-9" in prefix
    assert "oom_score_adj" in prefix


def test_a_refused_command_is_left_as_written(tmp_path: Path) -> None:
    result, command = _guard_call(tmp_path, "git rebase -i HEAD~2")
    assert result is not None and result["block"] is True
    assert command == "git rebase -i HEAD~2"


def test_a_broken_guard_or_rewrite_fails_open_and_is_logged(tmp_path: Path) -> None:
    """Only exit 2 refuses. A guard that crashes, and a rewrite script that is missing,
    let the command run as written, and each leaves a line in the state dir's log."""
    work_dir = tmp_path / "work"
    scripts = work_dir / "system" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "agent_prevent_commit_rewrite.sh").write_text(
        "echo 'guard crashed' >&2\nexit 1\n"
    )
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    payload = {"toolName": "bash", "input": {"command": "git rebase -i HEAD~2"}}
    out = _event_output(
        _run_event(
            tmp_path,
            _POLICY_GUARDS,
            "tool_call",
            payload,
            work_dir=work_dir,
            env={"MNGR_AGENT_STATE_DIR": str(state_dir)},
        )
    )
    assert out["result"] is None
    assert out["payload"]["input"]["command"] == "git rebase -i HEAD~2"
    log = (state_dir / "pi_policy_guards.log").read_text()
    assert (
        "agent_prevent_commit_rewrite.sh exited 1: guard crashed; failing open" in log
    )
    assert "agent_rewrite_bash_command.py exited 2" in log


def test_require_steps_reminder_rides_the_tool_result_when_no_step_is_in_progress(
    tmp_path: Path,
) -> None:
    result = _tk_result(
        tmp_path,
        "tool_result",
        {
            "toolName": "bash",
            "input": {"command": "echo hi"},
            "content": [{"type": "text", "text": "out"}],
        },
    )
    assert result is not None
    texts = [
        block["text"] for block in result["content"] if block.get("type") == "text"
    ]
    # The tool's own result is preserved, and the reminder is appended after it.
    assert texts[0] == "out"
    assert "[Step tracking reminder]" in texts[-1]
    assert "without declaring any step records" in texts[-1]


def test_require_steps_reminder_names_the_declared_but_unstarted_case(
    tmp_path: Path,
) -> None:
    result = _tk_result(
        tmp_path,
        "tool_result",
        {"toolName": "bash", "input": {"command": "echo hi"}, "content": []},
        steps="wor-1 [open] - a thing",
    )
    assert result is not None
    assert "none is currently in_progress" in result["content"][-1]["text"]


@pytest.mark.parametrize(
    ("payload", "in_progress"),
    [
        pytest.param(
            {"toolName": "bash", "input": {"command": "echo hi"}},
            "wor-1 [in_progress] - x",
            id="step-running",
        ),
        pytest.param({"toolName": "read", "input": {}}, "", id="read-only-tool"),
        pytest.param(
            {"toolName": "bash", "input": {"command": "tk create --step 'x'"}},
            "",
            id="tk-command",
        ),
    ],
)
def test_require_steps_stays_silent(
    tmp_path: Path, payload: dict[str, Any], in_progress: str
) -> None:
    payload = {**payload, "content": [{"type": "text", "text": "out"}]}
    assert (
        _tk_result(
            tmp_path, "tool_result", payload, in_progress=in_progress, steps=in_progress
        )
        is None
    )


def test_carryover_appends_open_steps_to_the_turns_system_prompt(
    tmp_path: Path,
) -> None:
    result = _tk_result(
        tmp_path,
        "before_agent_start",
        {"systemPrompt": "BASE_PROMPT"},
        steps="wor-9 [open] - unfinished thing",
    )
    assert result is not None
    assert result["systemPrompt"].startswith("BASE_PROMPT")
    assert "[Open task reminder" in result["systemPrompt"]
    assert "wor-9 [open] - unfinished thing" in result["systemPrompt"]


def test_carryover_is_silent_when_no_steps_are_open(tmp_path: Path) -> None:
    assert _tk_result(tmp_path, "before_agent_start", {"systemPrompt": "BASE"}) is None


def test_the_spawned_tk_child_reads_the_tickets_dir_the_handler_resolved(
    tmp_path: Path,
) -> None:
    """With TICKETS_DIR unset the handler resolves the WORK_DIR fallback for its own
    existence gate, and must hand that SAME dir to the tk it spawns -- as the shell hooks
    it mirrors do -- or the child re-resolves by parent-walk and can consult a different
    tickets dir than the one that was checked. The stub echoes back what it received."""
    work_dir = _tk_work_dir(tmp_path)
    result = _event_result(
        _run_event(
            tmp_path,
            _TK_WORKFLOW,
            "before_agent_start",
            {"systemPrompt": "BASE"},
            work_dir=work_dir,
            env={"STUB_ECHO_TICKETS_DIR": "1"},
        )
    )
    assert result is not None
    assert f"tickets_dir={work_dir / '.tickets'}" in result["systemPrompt"]


@pytest.mark.parametrize(
    ("steps", "expected"),
    [
        pytest.param(
            "wor-1 [open] - a\nwor-2 [open] - b",
            "Stopping with 2 step record(s) still open",
            id="two-open",
        ),
        pytest.param("", "", id="none-open"),
    ],
)
def test_stop_nudge_reports_open_steps_on_stderr(
    tmp_path: Path, steps: str, expected: str
) -> None:
    """agent_settled is the only stop-time channel pi offers, and it is stderr-only."""
    work_dir = _tk_work_dir(tmp_path)
    proc = _run_event(
        tmp_path,
        _TK_WORKFLOW,
        "agent_settled",
        {},
        work_dir=work_dir,
        env={"STUB_INPROGRESS": "", "STUB_STEPS": steps},
    )
    assert proc.returncode == 0, proc.stderr
    if expected:
        assert expected in proc.stderr
    else:
        assert "Stopping with" not in proc.stderr
