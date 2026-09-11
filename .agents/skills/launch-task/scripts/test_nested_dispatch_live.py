"""One live, two-level worker dispatch against REAL claude agents.

The contract tests (``create_worker_test.py``, ``dispatch_contract_test.py``) prove the
level-agnostic dispatch contract on argv and prose without ever starting an agent. This
release test proves the same contract against reality, end to end and exactly once: a
real ``worker``-template claude agent (the *outer* worker) reads the launch-task
procedure out of its own checkout, dispatches a *second* real worker (the *inner* one),
answers the mid-flight ``question`` gate the inner raises, merges the inner's branch,
and reports ``done`` to a top-level lead.

What only a live run can show, and what this asserts:

- **A nested dispatch is not mistaken for a dead one.** The lead-side ``await`` runs
  with a 3s poll interval, so the outer worker sits in WAITING -- turn ended, waiting on
  its child -- for dozens of consecutive polls. Child-aware idle detection is the only
  thing keeping that from returning ``_AWAIT_IDLE_RC`` (76); an exit code of 0 with a
  report is therefore a positive result for it.
- **The lead/worker edge is real at both levels.** The inner agent carries
  ``lead_agent=<outer>`` and the outer ``lead_agent=<top>`` in ``mngr list`` -- the
  labels ``launch`` attaches, which the idle check and the report fallback both read.
- **Paths never collide across levels.** Each level's task file, runtime dir, and report
  path are stamped exactly, and each report is archived into its own ``consumed/`` once.
- **A ``question`` round trip crosses a level.** The answer the outer sends reaches the
  inner and comes back as line 3 of a committed marker file.
- **Merge is a strict tree.** ``mngr/<inner>`` is merged only into ``mngr/<outer>``, so
  the top-level lead sees one branch carrying both markers.
- **Teardown carries the subtree's evidence upward.** The outer worker destroys the
  inner one after merging it (per ``lead-proxy.md``), so the inner is gone from
  ``mngr list`` and its transcript sits under the isolated host dir's ``preserved/``;
  when this harness then destroys the outer with the launcher, the inner's runtime dir
  -- task file and consumed ``done`` report, which lived only in the outer's worktree
  -- lands in the top-level tree at the same ``data/.tasks/launch-task/<inner>/`` path,
  and the outer's branch still carries both markers.

Isolation follows ``system/apps/chat/imbue/chat/test_message_conservation_release.py``:
an isolated ``MNGR_HOST_DIR`` with its own profile (opted into pytest, docker and modal
disabled), an isolated tmux server via ``TMUX_TMPDIR``, and an environment stripped of
the surrounding agent's own ``MNGR_*``/``TMUX``, so nothing here touches the workspace's
real agents. The work repo is a throwaway clone of this repo under the gitignored
``.test_output/`` -- the workers need this repo's skills, scripts, and
``.mngr/settings.toml``, and mngr refuses an ``--no-connect`` create whose source repo
the real claude config does not trust, which ``.test_output/`` inherits from the
checkout. Skipped cleanly whenever the environment cannot run a live claude agent.
"""

from __future__ import annotations

import functools
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import tomllib
import uuid
from pathlib import Path
from typing import Mapping

import pytest
from imbue.mngr.utils.polling import wait_for

pytestmark = pytest.mark.release

# .agents/skills/launch-task/scripts/<this file> -> the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[4]

_SCRIPT = Path(__file__).parent / "create_worker.py"
_spec = importlib.util.spec_from_file_location("create_worker_live", _SCRIPT)
assert _spec is not None and _spec.loader is not None
create_worker_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(create_worker_mod)

# Provisioning a `worker` agent runs a full `uv sync --all-packages` and a plugin
# install, and this flow does it twice (outer, then inner) on top of two claude turns
# per level plus a gate round trip. The budgets are generous but bounded; the pytest
# timeout is the outer backstop.
_MNGR_COMMAND_TIMEOUT_SECONDS = 120.0
_CREATE_TIMEOUT_SECONDS = 600.0
_DESTROY_TIMEOUT_SECONDS = 240.0
_REPORT_TIMEOUT_SECONDS = 1500.0
# Short on purpose: the outer worker spends most of the run parked in WAITING while its
# own child works, so a tight interval maximizes the number of polls that child-aware
# idle detection has to survive before the report lands.
_REPORT_POLL_INTERVAL_SECONDS = 3.0
_LISTING_SETTLE_TIMEOUT_SECONDS = 120.0

# The top-level lead is a `command` agent that does nothing but stay alive so it has a
# work dir the outer worker's report can be pushed to. A distinctive duration keeps the
# process from colliding with any other `sleep` on the host.
_TOP_AGENT_SLEEP_SECONDS = 86423

# The answer the outer worker sends back down through the inner worker's `question`
# gate. It reaches the assertions only by travelling outer task file -> outer agent ->
# `mngr message` -> inner agent -> committed marker file, so line 3 of that marker is a
# genuine end-to-end read of the gate round trip.
_GATE_ANSWER = "alpha"

_LIVE_CHILD_MERGE_SUBJECT = "Merge {inner}"
_INNER_MARKER_COMMIT_SUBJECT = "inner marker"
_OUTER_MARKER_COMMIT_SUBJECT = "outer marker"


def _is_ancestor_trusted_by_claude(path: Path) -> bool:
    """Whether ``path`` (or an ancestor) is trusted in the REAL ``~/.claude.json``.

    mngr's ``--no-connect`` create refuses an untrusted source repo, so without this the
    test cannot run. Mirrors mngr's own ancestor-walking trust check, read-only.
    """
    config_file = Path.home() / ".claude.json"
    if not config_file.is_file():
        return False
    try:
        config = json.loads(config_file.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    projects = config.get("projects", {})
    if not isinstance(projects, dict):
        return False
    resolved = path.resolve()
    for candidate in (resolved, *resolved.parents):
        project = projects.get(str(candidate))
        if isinstance(project, dict) and project.get("hasTrustDialogAccepted") is True:
            return True
    return False


def _claude_is_logged_in() -> bool:
    """Whether the real claude CLI holds a usable credential.

    Two shapes, because the credential store is platform-dependent: a container writes
    ``~/.claude/.credentials.json``, while a desktop keeps the token in the OS keychain
    and only ``claude auth status`` can see it. Either one means a live turn can run,
    so this is a capability probe, not a platform check.
    """
    if (Path.home() / ".claude" / ".credentials.json").is_file():
        return True
    try:
        result = subprocess.run(
            ["claude", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    try:
        status = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False
    return isinstance(status, dict) and status.get("loggedIn") is True


def _installed_claude_version() -> str | None:
    """The version ``claude --version`` reports (e.g. "2.1.267 (Claude Code)")."""
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    parts = result.stdout.split()
    return parts[0] if parts else None


def _skip_unless_live_nested_dispatch_possible(work_repo_parent: Path) -> None:
    """Skip unless every capability a two-level live dispatch needs is present."""
    if shutil.which("mngr") is None:
        pytest.skip("mngr CLI not on PATH")
    if shutil.which("claude") is None:
        pytest.skip("claude binary not on PATH")
    if _installed_claude_version() is None:
        pytest.skip("`claude --version` did not answer; a live worker cannot start")
    if shutil.which("tmux") is None:
        pytest.skip("tmux not on PATH; mngr runs every agent in a tmux session")
    if not _claude_is_logged_in():
        pytest.skip("no usable claude credential; a live worker turn cannot run")
    if not _is_ancestor_trusted_by_claude(work_repo_parent):
        pytest.skip(
            "no claude-trusted ancestor for the work repo; mngr create --no-connect "
            "would refuse"
        )


def _prepare_isolated_host_dir(host_dir: Path) -> None:
    """An isolated mngr host dir with its own profile, opted into pytest, local only."""
    profile_dir = host_dir / "profiles" / "nested-dispatch"
    profile_dir.mkdir(parents=True)
    (host_dir / "config.toml").write_text('profile = "nested-dispatch"\n')
    (profile_dir / "settings.toml").write_text(
        "is_allowed_in_pytest = true\n\n"
        "[providers.modal]\nis_enabled = false\n\n"
        "[providers.docker]\nis_enabled = false\n"
    )
    (profile_dir / "tmux_onboarding_shown").write_text("")


def _mngr_env(host_dir: Path, tmux_dir: Path) -> dict[str, str]:
    """The subprocess env for the mngr calls this harness makes directly.

    The caller's env minus its own agent identity (an ``MNGR_AGENT_NAME`` inherited from
    the agent running the suite would make the top-level lead look like somebody's
    worker), re-homed to the isolated host dir and tmux server and placed outside any
    enclosing tmux client.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("MNGR_") and key != "TMUX"
    }
    env["MNGR_HOST_DIR"] = str(host_dir)
    env["TMUX_TMPDIR"] = str(tmux_dir)
    return env


def _run_mngr(
    args: list[str],
    env: Mapping[str, str],
    cwd: Path,
    timeout: float = _MNGR_COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["mngr", *args],
        env=dict(env),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert result.returncode == 0, (
        f"mngr {args} failed (rc={result.returncode}):\n{result.stderr}\n{result.stdout}"
    )
    return result


def _run_git(args: list[str], cwd: Path, timeout: float = 300.0) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert result.returncode == 0, (
        f"git {args} failed (rc={result.returncode}):\n{result.stderr}\n{result.stdout}"
    )
    return result.stdout


def _agent_records(
    env: Mapping[str, str], cwd: Path
) -> tuple[Mapping[str, object], ...]:
    """Every agent record from one ``mngr list --format jsonl`` in the isolated host."""
    result = _run_mngr(
        ["list", "--format", "jsonl", "--on-error", "continue"], env, cwd
    )
    records: list[Mapping[str, object]] = []
    for line in result.stdout.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("resource_type") == "agent":
            records.append(record)
    return tuple(records)


def _record_named(
    records: tuple[Mapping[str, object], ...], name: str
) -> Mapping[str, object] | None:
    return next((r for r in records if r.get("name") == name), None)


def _label(record: Mapping[str, object], key: str) -> str | None:
    labels = record.get("labels")
    if not isinstance(labels, dict):
        return None
    value = labels.get(key)
    return value if isinstance(value, str) else None


def _reporting_section(worker_name: str) -> str:
    """The launch-task task-file template's reporting section, for one worker.

    Both task files in this dispatch carry it, because both workers are ordinary workers
    that report through the launcher: the whole point of the level-agnostic contract is
    that the intermediate lead's own reporting instructions are the same ones it hands
    down to its child.
    """
    return f"""## Reporting back

Follow `.agents/shared/references/worker-reporting.md` for the full report
procedure: parse this task's frontmatter for `TASK_FILE` / `LEAD_AGENT` /
`FINISH_REPORT_PATH`, then write your report body to a file and deliver it with
the launcher's `report` subcommand, which writes the report and pushes it to the
lead for you:

```bash
uv run .agents/skills/launch-task/scripts/create_worker.py report \\
    --task-file "$TASK_FILE" \\
    --type <gate|status> \\
    --name <name> \\
    --body-file <body-file>
```

Valid `name:` values: `question` (a mid-flight gate, valid at any point of any
run), `done` / `stuck` (terminal).

For a mid-flight `question` gate, stop your turn after reporting -- the lead
replies via `mngr message` and you resume. For terminal statuses, the run ends.
A `done` body names the branch, e.g. "Committed on branch `mngr/{worker_name}`.
Ready to merge."
"""


def _inner_task_body_spec(inner_name: str) -> str:
    """The prose the outer worker must turn into the inner worker's task body.

    Spelled out step by step because every one of these steps is load-bearing for an
    assertion: the gate must come first (so the outer really has to answer it), the
    marker must name both the worker and its stamped ``LEAD_AGENT`` (so the label and
    the stamp are read back from a live agent rather than from the launcher's own
    output), and the commit subjects are what the merged branch is checked for.
    """
    return f"""1. Parse its own task frontmatter. The `task_file` value stamped in the
   frontmatter it was sent is its exact path -- no globbing, no searching:

   ```bash
   eval "$(uv run .agents/shared/scripts/parse_task_frontmatter.py <that exact path>)"
   ```

2. **Before doing anything else**, ask which suffix it should use for its
   marker, as a `question` gate, and then stop its turn and wait for the reply:

   ```bash
   uv run .agents/skills/launch-task/scripts/create_worker.py report \\
       --task-file "$TASK_FILE" \\
       --type gate \\
       --name question \\
       --body-file <a file holding the one-sentence question>
   ```

3. When the answer arrives as a message, write `poc/markers/inner.txt` in its
   own worktree with exactly three lines and nothing else:

   - line 1: its own agent name (`$MNGR_AGENT_NAME`), which is `{inner_name}`
   - line 2: the `LEAD_AGENT` value from its own task frontmatter, verbatim
   - line 3: the suffix it was just given, verbatim

4. Commit it, with exactly this commit subject:

   ```bash
   git add poc && git commit -m "{_INNER_MARKER_COMMIT_SUBJECT}"
   ```

5. Report `done`, with a body naming its branch `mngr/{inner_name}`:

   ```bash
   uv run .agents/skills/launch-task/scripts/create_worker.py report \\
       --task-file "$TASK_FILE" \\
       --type status \\
       --name done \\
       --body-file <a file holding the one-line done body>
   ```
"""


def _outer_task_file_text(outer_name: str, inner_name: str) -> str:
    """The complete task file the harness hands the outer worker.

    Only ``finish_report_path`` is authored here; ``launch`` stamps ``task_file`` and
    ``lead_agent`` before sending it, which is exactly the contract under test.
    """
    merge_subject = _LIVE_CHILD_MERGE_SUBJECT.format(inner=inner_name)
    return f"""---
finish_report_path: data/.tasks/launch-task/{outer_name}/reports/report.md
---

# Task: prove a two-level dispatch

You are the OUTER worker of a nested-dispatch test. Your entire job is to
dispatch one sub-worker, answer its single question, merge its branch, leave a
marker of your own, and report. Do not touch anything else in this checkout, do
not run the test suite, and do not run any review gate.

**You have no user on this run.** Nobody will answer a question you ask, so
never stop to ask one. If something genuinely blocks you, report `stuck` with a
one-sentence reason (see "Reporting back" below) and end the run.

## Step 1 -- read the dispatch procedure

Read `.agents/skills/launch-task/SKILL.md` in this checkout and follow it for
Steps 2-5 below. Skip its Step 0 (`tk`): this run has no chat timeline.

## Step 2 -- write the sub-worker's task file

The sub-worker is named exactly `{inner_name}` and its runtime dir is
`data/.tasks/launch-task/{inner_name}/`.

```bash
mkdir -p data/.tasks/launch-task/{inner_name}/reports
```

Write `data/.tasks/launch-task/{inner_name}/task.md`. Its frontmatter must
contain exactly one field, and nothing else:

```
---
finish_report_path: data/.tasks/launch-task/{inner_name}/reports/report.md
---
```

Do NOT write `task_file` or `lead_agent` yourself -- `launch` stamps both.

Write the body in your own words, but it must instruct the sub-worker to do all
of the following, in this order:

{_inner_task_body_spec(inner_name)}
End the sub-worker's task body with the section between the `~~~` markers
below, verbatim (drop the markers themselves), so it holds the whole reporting
contract in its own task file:

~~~
{_reporting_section(inner_name)}~~~

## Step 3 -- launch the sub-worker

Your working tree is clean and stays clean: everything you just wrote lives
under gitignored `data/`.

```bash
uv run .agents/skills/launch-task/scripts/create_worker.py launch \\
    --name {inner_name} \\
    --template worker \\
    --runtime-dir data/.tasks/launch-task/{inner_name}/ \\
    --task-file data/.tasks/launch-task/{inner_name}/task.md
```

## Step 4 -- poll, answer the question, re-arm

Run the poll as a **background** Bash task and end your turn; handle each report
when the background job comes back.

```bash
uv run .agents/skills/launch-task/scripts/create_worker.py await \\
    --name {inner_name} \\
    --task-file data/.tasks/launch-task/{inner_name}/task.md \\
    --timeout 20m \\
    --poll-interval 5s
```

When the `question` gate arrives, answer it with exactly the word `{_GATE_ANSWER}`
and nothing else:

```bash
mngr message {inner_name} -m "{_GATE_ANSWER}"
```

Then re-arm the same background `await`. You never move a report by hand: the
`await` that printed it already archived it under
`data/.tasks/launch-task/{inner_name}/reports/consumed/`.

## Step 5 -- merge, destroy the sub-worker, mark, report -- all in one turn

When the `done` status arrives, do Step 5, Step 6 and the final report **in a
single turn, without ending your turn in between**. Your lead is polling you and
treats a turn that ends with no live sub-worker and no report as a stalled run.

```bash
git merge --no-ff mngr/{inner_name} -m "{merge_subject}"
uv run .agents/skills/launch-task/scripts/create_worker.py destroy --name {inner_name}
```

The merge must be `--no-ff` and its subject must be exactly `{merge_subject}`.
Destroy the sub-worker with that exact command (not a raw `mngr destroy`), and
do NOT destroy yourself -- your own lead does that.

## Step 6 -- your own marker

Write `poc/markers/outer.txt` in your worktree with exactly two lines and
nothing else:

- line 1: your own agent name (`$MNGR_AGENT_NAME`), which is `{outer_name}`
- line 2: the `LEAD_AGENT` value from **this** task file's frontmatter, verbatim

Then commit it with exactly this commit subject:

```bash
git add poc && git commit -m "{_OUTER_MARKER_COMMIT_SUBJECT}"
```

Then report `done` (see below), with a body naming your branch
`mngr/{outer_name}`.

## Success criteria

- `mngr/{outer_name}` carries `{_INNER_MARKER_COMMIT_SUBJECT}` (via the merge),
  `{merge_subject}`, and `{_OUTER_MARKER_COMMIT_SUBJECT}`.
- `poc/markers/inner.txt` and `poc/markers/outer.txt` both exist on your branch
  with exactly the contents described above.
- `{inner_name}` no longer appears in `mngr list` (destroyed by the launcher).

{_reporting_section(outer_name)}"""


def _pin_claude_to_the_installed_version(settings: Path, installed: str) -> None:
    """Point the clone's ``[agent_types.claude] version`` at the claude on this machine.

    The committed pin is enforced at provisioning time -- mngr refuses to start a
    claude agent whose installed binary diverges from it -- and it exists so a
    workspace's workers run the build the workspace was set up with. This test wants
    the opposite: to run wherever *a* claude can run, on a laptop whose build has
    drifted as much as in a workspace on the pin. So the clone's pin follows the
    installed version. Rewritten in place, like the worktree root, and asserted through
    a real TOML parse so a moved or renamed key fails here rather than as a refused
    ``mngr create``.
    """
    lines = settings.read_text(encoding="utf-8").splitlines(keepends=True)
    replaced = 0
    for index, line in enumerate(lines):
        if line.startswith("version = "):
            lines[index] = f'version = "{installed}"\n'
            replaced += 1
    assert replaced == 1, (
        f"expected exactly one `version = ...` line in {settings}, found {replaced}"
    )
    settings.write_text("".join(lines), encoding="utf-8")
    pinned = tomllib.loads(settings.read_text(encoding="utf-8"))["agent_types"][
        "claude"
    ]["version"]
    assert pinned == installed, (
        f"the rewritten pin is {pinned!r}, not the installed {installed!r}; the "
        "`version` line rewritten above is not the one under [agent_types.claude]"
    )


def _isolate_worktree_base(settings: Path, worktree_base: Path) -> None:
    """Point the clone's ``worktree_base_folder`` at a throwaway dir.

    The committed value is the workspace's own shared worktree root, which every real
    worker on the host uses; a test that borrowed it would drop two agents' worktrees in
    among the user's. Rewritten in place rather than prepended, because a second
    top-level key of the same name is a TOML duplicate-key error -- and asserted, so a
    rename upstream fails here loudly instead of silently leaving the shared root in use.
    """
    lines = settings.read_text(encoding="utf-8").splitlines(keepends=True)
    replaced = 0
    for index, line in enumerate(lines):
        if line.startswith("worktree_base_folder"):
            lines[index] = f'worktree_base_folder = "{worktree_base}"\n'
            replaced += 1
    assert replaced == 1, (
        f"expected exactly one top-level `worktree_base_folder` in {settings}, "
        f"found {replaced}"
    )
    settings.write_text("".join(lines), encoding="utf-8")


def _clone_repo_at_head(
    work_repo_parent: Path, suffix: str, worktree_base: Path, installed_claude: str
) -> Path:
    """A throwaway clone of this repo, on this branch, under gitignored ``.test_output``.

    The workers need this repo's own skills, scripts, and project ``.mngr/settings.toml``
    (the `worker` template lives there), so an empty scratch repo would not do -- but
    they must not run in the checkout the suite is running from. A local clone hardlinks
    the object store, so this costs a couple of seconds and no meaningful disk.

    Three edits are made to the clone's project config and committed (``launch`` refuses
    a dirty tree): the pytest opt-in mngr demands of every config it loads, the
    worktree root the two workers will be given, and the claude version pin, which
    follows the claude installed here.
    """
    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], _REPO_ROOT).strip()
    clone = work_repo_parent / f"nested-dispatch-repo-{suffix}"
    _run_git(
        ["clone", "--quiet", "--branch", branch, str(_REPO_ROOT), str(clone)],
        work_repo_parent,
        timeout=600.0,
    )
    # Both workers commit, so the clone needs an identity of its own rather than
    # depending on whatever global git config the host happens to carry.
    _run_git(["config", "user.email", "nested-dispatch@example.invalid"], clone)
    _run_git(["config", "user.name", "Nested Dispatch Test"], clone)

    # mngr refuses to run under pytest unless EVERY config file it loads opts in, and
    # the clone's project config is one of them -- it is the whole reason the work repo
    # is a clone of this repo rather than an empty scratch one (the `worker` template
    # lives there). Prepended, not appended: a bare key after the last `[table]` would
    # land inside that table.
    settings = clone / ".mngr" / "settings.toml"
    settings.write_text(
        "# Set by test_nested_dispatch_live.py: this throwaway clone is driven from a\n"
        "# pytest process, and mngr requires every loaded config to opt in.\n"
        "is_allowed_in_pytest = true\n\n" + settings.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    _isolate_worktree_base(settings, worktree_base)
    _pin_claude_to_the_installed_version(settings, installed_claude)
    _run_git(["add", ".mngr/settings.toml"], clone)
    _run_git(
        ["commit", "--quiet", "-m", "Isolate this test clone's mngr config"], clone
    )
    return clone


@pytest.mark.timeout(1800, func_only=False)
def test_live_nested_dispatch_merges_both_levels_after_a_gate_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_repo_parent = _REPO_ROOT / ".test_output"
    work_repo_parent.mkdir(exist_ok=True)
    _skip_unless_live_nested_dispatch_possible(work_repo_parent)

    host_dir = tmp_path / "host"
    _prepare_isolated_host_dir(host_dir)
    # NOT under tmp_path: a unix socket path is capped at ~104 bytes on macOS, and
    # pytest's own tmp_path (`.../pytest-of-<user>/pytest-N/<test name truncated to 30>/`)
    # plus tmux's `tmux-<uid>/default` overruns that, so the create dies with "File name
    # too long". mkdtemp keeps the isolated server's socket short on both platforms.
    tmux_dir = Path(tempfile.mkdtemp(prefix="nested-tmux-"))
    env = _mngr_env(host_dir, tmux_dir)

    suffix = uuid.uuid4().hex[:12]
    top_name = f"nested-top-{suffix}"
    outer_name = f"nested-outer-{suffix}"
    inner_name = f"nested-inner-{suffix}"

    # The two workers' worktrees, kept out of the workspace's shared worktree root.
    worktree_base = Path(tempfile.mkdtemp(prefix="nested-worktrees-"))
    installed_claude = _installed_claude_version()
    assert installed_claude is not None  # the skip check above already required it
    clone = _clone_repo_at_head(
        work_repo_parent, suffix, worktree_base, installed_claude
    )
    outer_runtime_dir = Path("data") / ".tasks" / "launch-task" / outer_name
    outer_task_file = outer_runtime_dir / "task.md"
    outer_report_path = clone / outer_runtime_dir / "reports" / "report.md"

    try:
        # The top-level lead: a `command` agent running in place, so its work dir IS the
        # clone and the outer worker's report push lands where this harness polls.
        _run_mngr(
            [
                "create",
                top_name,
                "--type",
                "command",
                "--transfer=none",
                "--no-connect",
                "--",
                "sleep",
                str(_TOP_AGENT_SLEEP_SECONDS),
            ],
            env,
            clone,
            timeout=_CREATE_TIMEOUT_SECONDS,
        )

        (clone / outer_runtime_dir / "reports").mkdir(parents=True)
        (clone / outer_task_file).write_text(
            _outer_task_file_text(outer_name, inner_name), encoding="utf-8"
        )

        # The in-process launcher calls run real subprocesses, so they need the same
        # isolation the direct mngr calls above got -- plus MNGR_AGENT_NAME, which is
        # what makes this harness the top-level lead the outer worker is stamped and
        # labelled with.
        for key in list(os.environ):
            if key.startswith("MNGR_"):
                monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv("TMUX", raising=False)
        monkeypatch.setenv("MNGR_HOST_DIR", str(host_dir))
        monkeypatch.setenv("TMUX_TMPDIR", str(tmux_dir))
        monkeypatch.setenv("MNGR_AGENT_NAME", top_name)
        # Relative paths, deliberately: `rsync_dir` addresses the worker endpoint with
        # the same repo-relative string, so an absolute runtime dir would push the
        # runtime outside the worker's worktree.
        monkeypatch.chdir(clone)

        launch_rc = create_worker_mod.launch(
            name=outer_name,
            template="worker",
            runtime_dir=outer_runtime_dir,
            task_file=outer_task_file,
            runner=create_worker_mod.Runner(),
        )
        assert launch_rc == 0, f"launching the outer worker failed (rc={launch_rc})"

        report_output = io.StringIO()
        await_rc = create_worker_mod.await_report(
            outer_report_path,
            timeout_seconds=_REPORT_TIMEOUT_SECONDS,
            poll_interval_seconds=_REPORT_POLL_INTERVAL_SECONDS,
            out=report_output,
            worker_name=outer_name,
            pending_shed_check=create_worker_mod._worker_has_pending_shed,
            idle_check=functools.partial(
                create_worker_mod._worker_is_idle, runner=create_worker_mod.Runner()
            ),
        )
        report_text = report_output.getvalue()
        # 76 here would mean the child-aware idle check counted the outer worker as
        # finished while it was merely waiting on the inner one; 124 a timeout; 75 an
        # OOM shed.
        assert await_rc == 0, (
            f"await returned {await_rc} instead of a report from {outer_name}. "
            f"Anything printed:\n{report_text}"
        )

        report = create_worker_mod.parse_report(report_text)
        assert (report.report_type, report.name) == ("status", "done"), (
            f"expected a terminal `done` status from {outer_name}, got "
            f"type={report.report_type!r} name={report.name!r}. Report:\n{report_text}"
        )

        consumed_dir = outer_report_path.parent / "consumed"
        archived = (
            sorted(p.name for p in consumed_dir.iterdir())
            if consumed_dir.is_dir()
            else []
        )
        assert len(archived) == 1, (
            f"expected exactly one archived report in {consumed_dir}, found {archived}. "
            f"Report:\n{report_text}"
        )
        assert archived[0].endswith("-status-done.md"), (
            f"the archived report is not named for its kind: {archived[0]!r}. "
            f"Report:\n{report_text}"
        )

        outer_branch = f"mngr/{outer_name}"
        subjects = _run_git(["log", "--format=%s", outer_branch], clone).splitlines()
        merge_subject = _LIVE_CHILD_MERGE_SUBJECT.format(inner=inner_name)
        for expected in (
            merge_subject,
            _OUTER_MARKER_COMMIT_SUBJECT,
            _INNER_MARKER_COMMIT_SUBJECT,
        ):
            assert expected in subjects, (
                f"{expected!r} is missing from {outer_branch}; the branch carries "
                f"{subjects[:20]}. Report:\n{report_text}"
            )

        inner_marker = _run_git(
            ["show", f"{outer_branch}:poc/markers/inner.txt"], clone
        )
        assert inner_marker.strip().splitlines() == [
            inner_name,
            outer_name,
            _GATE_ANSWER,
        ], (
            f"the inner worker's marker does not name itself, its lead, and the "
            f"answer it was given: {inner_marker!r}. Report:\n{report_text}"
        )

        outer_marker = _run_git(
            ["show", f"{outer_branch}:poc/markers/outer.txt"], clone
        )
        assert outer_marker.strip().splitlines() == [outer_name, top_name], (
            f"the outer worker's marker does not name itself and its lead: "
            f"{outer_marker!r}. Report:\n{report_text}"
        )

        # The sub-worker was destroyed by its lead after the merge: gone from the
        # listing, its transcript preserved by mngr under the isolated host dir.
        def _inner_is_gone() -> bool:
            return _record_named(_agent_records(env, clone), inner_name) is None

        wait_for(
            _inner_is_gone,
            timeout=_LISTING_SETTLE_TIMEOUT_SECONDS,
            poll_interval=3.0,
            error_message=(
                f"{inner_name} is still listed after its lead merged and destroyed it"
            ),
        )
        preserved = sorted((host_dir / "preserved").glob(f"{inner_name}--*"))
        assert len(preserved) == 1, (
            f"expected one preserved dir for {inner_name} under {host_dir / 'preserved'}, "
            f"found {preserved}"
        )
        assert any(preserved[0].rglob("*.jsonl")), (
            f"{preserved[0]} holds no transcript (no .jsonl underneath it)"
        )

        # The labels the outer level was created with, read back off the live
        # listing: its lead, and the runtime dir the top-level destroy below
        # excludes from its pull.
        outer_record = _record_named(_agent_records(env, clone), outer_name)
        assert outer_record is not None, f"{outer_name} is missing from `mngr list`"
        assert _label(outer_record, "lead_agent") == top_name, (
            f"{outer_name} should be labelled with its lead {top_name!r}, got "
            f"{_label(outer_record, 'lead_agent')!r}"
        )
        assert _label(outer_record, "runtime_dir") == outer_runtime_dir.as_posix(), (
            f"{outer_name} should be labelled with its runtime dir "
            f"{outer_runtime_dir.as_posix()!r}, got {_label(outer_record, 'runtime_dir')!r}"
        )

        # The top-level lead's own teardown, through the launcher: the outer goes,
        # and the inner's runtime dir -- which existed only in the outer's worktree
        # -- is carried into this tree at the same path first.
        inner_runtime_dir = clone / "data" / ".tasks" / "launch-task" / inner_name
        assert not inner_runtime_dir.exists(), (
            f"{inner_runtime_dir} exists before the outer's destroy; the relocation "
            "assertion below would be vacuous"
        )
        destroy_rc = create_worker_mod.destroy(outer_name, create_worker_mod.Runner())
        assert destroy_rc == 0, f"destroying {outer_name} exited {destroy_rc}"
        assert _record_named(_agent_records(env, clone), outer_name) is None, (
            f"{outer_name} is still listed after the launcher destroyed it"
        )
        assert (inner_runtime_dir / "task.md").is_file(), (
            f"the inner worker's task file was not relocated to {inner_runtime_dir}"
        )
        inner_consumed = sorted(
            p.name for p in (inner_runtime_dir / "reports" / "consumed").glob("*.md")
        )
        assert any(name.endswith("-status-done.md") for name in inner_consumed), (
            f"the inner worker's consumed `done` report was not relocated; "
            f"{inner_runtime_dir}/reports/consumed holds {inner_consumed}"
        )
        # Destroy keeps the branch: the merged tree is still readable.
        assert (
            _OUTER_MARKER_COMMIT_SUBJECT
            in _run_git(["log", "--format=%s", outer_branch], clone).splitlines()
        ), f"{outer_branch} did not survive the destroy"
    finally:
        # Best-effort, for the failure case: on success the inner and outer are
        # already gone. Children first, and from inside the clone: `.test_output/`
        # would resolve the *real* repo's project config, which does not opt into
        # pytest, so mngr would refuse to run at all and leave three live agents
        # behind.
        for name in (inner_name, outer_name, top_name):
            subprocess.run(
                ["mngr", "destroy", name, "--force"],
                env=env,
                cwd=str(clone),
                capture_output=True,
                text=True,
                timeout=_DESTROY_TIMEOUT_SECONDS,
                check=False,
            )
        socket_path = tmux_dir / f"tmux-{os.getuid()}" / "default"
        subprocess.run(
            ["tmux", "-S", str(socket_path), "kill-server"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        shutil.rmtree(clone, ignore_errors=True)
        shutil.rmtree(worktree_base, ignore_errors=True)
        shutil.rmtree(tmux_dir, ignore_errors=True)
