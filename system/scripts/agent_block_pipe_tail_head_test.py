"""A pipe into tail/head is blocked unless the output reaching it can be read again."""

import pytest
from guard_testing import run_guard


def _run(command: str) -> int:
    return run_guard(
        "agent_block_pipe_tail_head.sh",
        {"tool_name": "Bash", "tool_input": {"command": command}},
    )


@pytest.mark.parametrize(
    "command",
    [
        "cat notes.md | head -120",
        "cat a.md b.md 2>&1 | head -120",
        "cat log.txt|tail",
        "  cat out.txt | tail -n 20 > /tmp/last.txt",
        "cat build.log | tail -50 &>/tmp/last.txt",
        # Judged pipeline by pipeline, so a leading cd or a sibling command does not matter.
        "cd /tmp && cat out.txt | tail -20",
        "cd system/apps/x && ls -R . | head -60; echo '==='; cat README.md 2>/dev/null | head -40",
        "pytest > /tmp/out.txt 2>&1; grep -E 'passed|failed' /tmp/out.txt | tail -3",
        "git diff main...HEAD --stat | tail -40",
        "git -C ../other log --oneline main..HEAD | head -50",
        "git show HEAD:system/supervisord.conf | grep -A 20 '^\\[program:' | head -50",
        "git branch -a 2>&1 | head -50",
        "git branch --merged main | head",
        # reflog shows the log for any log option or ref, not only for `show`.
        "git reflog -n 20 | head",
        "git reflog main | head",
        # A `>(` inside a heredoc body is text, not a process substitution.
        "python3 - <<'PY' > /tmp/scan.txt\nre.search(r'<href>(.*?)</href>', x)\nPY\n"
        "sort -rn /tmp/scan.txt | head -30",
        "git tag -l 'minds-v0.4*' | head",
        "rg --files -g '*README*' docs | head -180",
        "find system/apps -maxdepth 2 -iname '*review*' 2>/dev/null | head",
        "grep -rn TODO system/scripts | head",
        # Naming the root is only a whole-filesystem walk for a recursive reader.
        "ls -la / | head",
        "df -h / | tail -1",
        "LC_ALL=C sort -u /tmp/names.txt | head -20",
        "cat f | head -5 | tail -2",
        "cat f | grep x | head",
        # A reserved word opening the stage is not the program that runs.
        "for f in *.md; do cat $f | head -5; done",
        "if true; then git log --oneline | head -3; fi",
        "time cat f | head",
        # A line continuation is not part of the program name.
        "cat f | \\\n  grep x | head",
        "git \\\n  log --oneline | head",
        # head/tail picking one value inside a command substitution.
        "INIT=$(git rev-list --first-parent HEAD | tail -1)",
        # `||` is not a pipe.
        "grep -E 'x' /tmp/log.txt || tail -4 /tmp/log.txt",
        # tee keeps the full output, so truncating what comes out of it loses nothing.
        "PYTEST_MAX_DURATION_SECONDS=300 uv run pytest -q 2>&1 | tee /tmp/pytest.txt | tail -30",
        "uv run mngr list --help 2>&1 | head -50",
        "uv run sh -c 'dmesg | tail -n 30'",
        # A read's quoted pattern is not a command, however pipe-shaped it is.
        "grep -nE 'error|tail' /tmp/log.txt",
        "rg 'foo|head' src | head -20",
    ],
)
def test_output_that_can_be_read_again_may_pipe_into_head_or_tail(command: str) -> None:
    assert _run(command) == 0


@pytest.mark.parametrize(
    "command",
    [
        "pytest | tail -20",
        "PYTEST_MAX_DURATION_SECONDS=180 uv run pytest -q 2>&1 | tail -30",
        "uv sync --all-packages 2>&1 | tail -5",
        "git fetch upstream --tags 2>&1 | tail -5",
        "git commit -q -m 'msg' 2>&1 | tail -3",
        "curl -s localhost:8098/ | head -c 300",
        "python3 -c 'print(1)' | head",
        "timeout 300 git log | head",
        "find / -name x.json 2>/dev/null | head -1",
        "du -sh /* 2>/dev/null | sort -h | tail -20",
        "grep -rl needle / 2>/dev/null | head",
        "rg needle / | head",
        "ls -lR / | head",
        # -exec runs a program per match, so its output is that program's.
        "find . -name '*_test.py' -exec pytest {} + | tail -20",
        "git branch -D old | head",
        "git stash | tail -1",
        "git reflog expire --expire=now --all | tail",
        # Queries the remote unless given -n.
        "git remote show origin | head",
        # The cat is reading pytest's output, not a file.
        "pytest | cat | tail -20",
        "pytest |& cat | tail -20",
        "pytest |& tail -20",
        "pytest > >(cat | tail -20)",
        "pytest |\ncat | tail -20",
        "pytest | \n\ncat | tail -20",
        "cat notes.md\npytest | tail -20",
        "uv run pytest -q 2>&1 | \\\n  tail -30",
        # An escaped backslash does not continue the line, so pytest starts a new command.
        "echo done\\\\\npytest | tail -5",
        "cat $(pytest) | head",
        'cat "$(pytest)" | head',
        "cat <(pytest) | head",
        "cat `pytest` | head",
        "echo `pytest | tail`",
        "(pytest) | head",
        # The lexer returns `)|` as one token; it still pipes the group into head.
        "(pytest)|head",
        "(cd x && pytest)|tail -5",
        "cat $(pytest)|head",
        "cat f | head; pytest | tail -5",
        "cat f && pytest | tail -20",
        "cat f & pytest | tail -20",
        "for f in a b; do pytest $f | tail -5; done",
        # The escaped `>` leaves `&` a background operator, not part of a `>&` redirect.
        "cat x\\>& pytest | tail -20",
        "concatenate f | head",
        "env pytest | tail",
        "find . -name '*.py' | xargs grep -l x | head",
        # Quoted shell text is judged as a command of its own.
        "bash -c 'pytest | tail -20'",
        "ssh host 'uv sync|tail'",
        'echo "$(pytest | tail -5)"',
        # A command the lexer cannot read is blocked, as before.
        "pytest | tail -20 'unbalanced",
    ],
)
def test_output_that_would_be_lost_is_blocked(command: str) -> None:
    assert _run(command) == 2
