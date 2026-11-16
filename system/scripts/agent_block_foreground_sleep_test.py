"""A foreground `sleep` of 5 seconds or more is blocked; shorter ones and background waits are not."""

import pytest
from workspace_scripts.guard_testing import run_guard


def _run(command: str, run_in_background: bool | None = None) -> int:
    tool_input: dict[str, object] = {"command": command}
    if run_in_background is not None:
        tool_input["run_in_background"] = run_in_background
    return run_guard(
        "agent_block_foreground_sleep.sh",
        {"tool_name": "Bash", "tool_input": tool_input},
    )


@pytest.mark.parametrize(
    "command",
    [
        # Polling a background task's output on a guessed schedule.
        "sleep 240; cat /tmp/claude-0/-mngr-vol-home-worktrees-update-self-667f/5a62/tasks/bw6fdqtms.output",
        "sleep 200; cat /tmp/out.txt; tail -3 /tmp/suite-pr_review.txt",
        "sleep 590",
        "cd /home/user/workspace && sleep 90; grep -i error /var/log/supervisor/work-list-stderr.log",
        "sleep 30 && cat /tmp/dd_full.txt",
        "sleep 1m; cat /tmp/out.txt",
        "sleep 2 3; cat /tmp/out.txt",  # sleep sums its arguments
        "sleep 30 > /dev/null 2>&1",
        "sleep infinity",
        "sleep inf; cat /tmp/out.txt",
        'sleep "30"; cat /tmp/out.txt',
        "sleep '30'",
        # Waiting out a service restart.
        "supervisorctl restart reading-room > /dev/null; sleep 8; supervisorctl status reading-room",
        "supervisorctl restart apartment-hunt > /tmp/rs.txt 2>&1\nsleep 8\ncurl -s localhost:8088/api/state",
        "uv run apartment-hunt > /tmp/ahboot.txt 2>&1 &\nBOOTPID=$!\nsleep 12\ncurl localhost:8098/",
        # A long sleep inside a loop or branch body still holds the turn.
        'for i in $(seq 1 55); do [ -z "$(git status --porcelain)" ] && exit 0; sleep 10; done',
        "for i in 1 2 3; do ./probe.py && break; sleep 20; done",
        "for t in 1 2 3; do sleep 8; curl -s https://example.com/$t; done",
        'case "$s" in *generating*) sleep 10;; *) echo done;; esac',
        "if pgrep -f build; then sleep 60; fi",
        "while pgrep -f recapture.py >/dev/null; do sleep 15; done",
        "{ sleep 30; cat /tmp/out.txt; }",
        "time sleep 30",
        # A subshell or command substitution runs in the foreground unless sent to the background.
        "(sleep 30); cat /tmp/out.txt",
        'out=$(sleep 30); echo "$out"',
        # Only the list a `&` ends goes to the background.
        "sleep 30 & sleep 40",
        "{ sleep 30; cat /tmp/out.txt; } &",
    ],
)
def test_a_foreground_sleep_of_five_seconds_or_more_is_blocked(command: str) -> None:
    assert _run(command) == 2


@pytest.mark.parametrize(
    "command",
    [
        # Short sleeps, including a condition poll that ends as soon as the state changes.
        "supervisorctl restart reading-room 2>&1; sleep 1; supervisorctl status reading-room",
        "supervisorctl update; sleep 2.5; curl -s localhost:8000/",
        "until curl -sf localhost:8080/; do sleep 1; done",
        "sleep 4.9",
        # A redirect's fd and target are not durations.
        "sleep 3 2>/dev/null",
        "sleep 2 2>&1",
        # A sleep handed to the background runner is the runner's, not this command's.
        "python3 system/scripts/workspace_bare_scripts/run_in_background.py --description 'Check back' -- bash -c 'sleep 300; cat /tmp/out.txt'",
        "python3 system/scripts/workspace_bare_scripts/run_in_background.py --description 'Check back' -- sleep 300",
        # A sleep in a list sent to the background holds no turn.
        "mkdir -p data/.tasks/mcp-login\n(sleep 900 | mcpc login https://mcp.example.com/mcp > data/.tasks/mcp-login/example.out 2>&1 &)",
        "sleep 600 &",
        "sleep infinity &",
        "(sleep 30) &",
        "sleep 30 && echo done &",
        # Mentions of sleep that run no sleep.
        "grep -nE 'mock|sleep' test_scoring.py",
        "git commit -m 'replace the overlap test sleep 30 with an event'",
        "echo 'sleep 60'",
        "python3 -c 'import time; time.sleep(30)'",
        "sleep $DELAY; cat /tmp/out.txt",
        "notsleep 30",
    ],
)
def test_a_short_sleep_or_one_that_is_only_an_argument_is_allowed(command: str) -> None:
    assert _run(command) == 0


def test_a_command_claude_runs_in_the_background_is_allowed() -> None:
    command = "sleep 900; uv run .agents/skills/launch-task/scripts/create_worker.py await --name w"
    assert _run(command, run_in_background=True) == 0
    assert _run(command, run_in_background=False) == 2
