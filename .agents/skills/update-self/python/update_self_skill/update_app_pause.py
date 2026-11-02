"""The apps an update stops to free memory, and how they come back.

``run-status stop-apps`` stops a supervisord program and records it on the run record before the
next one is touched; ``resume-apps`` starts each recorded program and drops it from the record as
it comes up. The record is the point: the pass that stops these apps is the one running out of
memory, so its own chat may be shed before it restores anything.

``supervisorctl``, not the control socket, because the staged copy must run on a workspace whose
own tree may predate any helper. Stdlib-only, like its siblings here.
"""

import subprocess
from collections.abc import Callable, Sequence
from typing import NamedTuple

# supervisorctl waits out a program's ``startsecs`` (30s for a scaffolded app) before it answers a
# start, so a handful of apps can take a few minutes between them.
STOP_TIMEOUT_SECONDS = 120.0
START_TIMEOUT_SECONDS = 180.0

# What supervisorctl prints for a program supervisord has no such name for, exiting 1. A redundant
# stop or start needs no special case: supervisorctl exits 0 for those.
_NO_SUCH_PROCESS = "no such process"


class SupervisorctlAnswer(NamedTuple):
    is_ok: bool
    is_timed_out: bool
    text: str


def run_supervisorctl(action: str, program: str, timeout: float) -> SupervisorctlAnswer:
    try:
        result = subprocess.run(
            ["supervisorctl", action, program],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return SupervisorctlAnswer(
            is_ok=False, is_timed_out=True, text=f"never answered within {timeout:g}s"
        )
    except OSError as e:
        return SupervisorctlAnswer(
            is_ok=False, is_timed_out=False, text=f"could not run supervisorctl: {e}"
        )
    return SupervisorctlAnswer(
        is_ok=result.returncode == 0,
        is_timed_out=False,
        text=(result.stdout or result.stderr or "").strip(),
    )


def stop_programs(
    programs: Sequence[str],
    record: Callable[[str, bool], None],
) -> list[str]:
    """Stop each program, calling ``record`` for every one this pass may now owe a start.

    ``record`` is called before the next program is touched, with whether supervisorctl confirmed
    the stop. A stop that timed out is recorded unconfirmed: the program may well be down, and
    leaving it off would leave nothing to bring it back. Any other failure means it is still up,
    so it is only reported. Returns one line per program that could not be stopped.
    """
    problems: list[str] = []
    for program in programs:
        answer = run_supervisorctl("stop", program, STOP_TIMEOUT_SECONDS)
        if not answer.is_ok:
            problems.append(f"could not stop {program}: {answer.text}")
            if not answer.is_timed_out:
                continue
        record(program, answer.is_ok)
    return problems


def start_programs(
    programs: Sequence[str],
    forget: Callable[[str, bool], None],
) -> list[str]:
    """Start each program again, calling ``forget`` for each one the record no longer needs.

    ``forget``'s second argument says whether the program is up. A program supervisord no longer
    has (the update removed or renamed it) is forgotten while down, and reported: no later resume
    could start it either. Any other failure stays on the record for a later ``resume-apps``.
    Returns one line per program that is not up.
    """
    problems: list[str] = []
    for program in programs:
        answer = run_supervisorctl("start", program, START_TIMEOUT_SECONDS)
        if answer.is_ok:
            forget(program, True)
            continue
        if _NO_SUCH_PROCESS in answer.text.lower():
            problems.append(
                f"{program} is no longer a program in this workspace, so it was not started "
                f"and is dropped from the record: {answer.text}"
            )
            forget(program, False)
            continue
        problems.append(f"could not start {program}: {answer.text}")
    return problems
