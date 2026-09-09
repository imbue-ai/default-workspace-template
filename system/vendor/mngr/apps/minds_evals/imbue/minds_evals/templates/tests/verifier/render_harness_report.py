"""Render the harness reports the `harness_quality` judges read, and count the failure signatures its
programmatic criteria score, at GRADE time from the trial's ATIF trajectories.

`harness_quality` asks a different question from the other dimensions: not how well the agent worked,
but whether the workspace it was given actually let it work. A skill that will not load, a plugin the
session cannot resolve, a browser the tests cannot find -- these look like agent failures in every
other dimension, because the agent visibly does not do the thing it said it would. Separating them
keeps a broken workspace from being scored as a bad agent, and makes a harness regression legible on
its own axis.

Two reports are written, because the two scopes fail differently and are fixed by different people:

- ``harness_main.txt`` from ``/logs/agent/trajectory.json`` -- the workspace agent the client talks to.
- ``harness_workers.txt`` from every ``/logs/agent/verification/workers/<name>/trajectory.json`` -- the
  agents it launched. Hardening, review gates and crystallization run here, so this is where a missing
  plugin usually bites.

Both are *processed*: a raw trajectory runs to hundreds of KB and would not fit a judge prompt, and
most of it is ordinary work. Each report keeps only the steps that bear on whether the harness held --
skill invocations, errored tool results, and anything matching a known failure signature -- with the
agent's own words for that step, so the judge can see whether the agent recovered or gave up.

Only output a tool actually *produced by running something* is classified, plus any errored result:
file contents an agent read and prose a subagent wrote quote the strings a broken harness prints, and
counting those scores an agent for the documentation it consulted.

``harness_failures.json`` carries the scripted side: per-scope counts of each signature, and the
``scored_total`` that ``harness_quality/checks.py`` scores without a model in the loop. The judge reads
prose and can weigh "tried three times and worked around it" against "gave up"; the counts cannot be
argued with. Neither is sufficient alone. Not every signature is scored -- see UNCOUNTED_SIGNATURES --
but all of them stay in the report, because a path the agent probed for and did not find is context
the judge should have even when it is not evidence of a broken harness.

Runs in the verifier container: stdlib only, absolute paths.
"""

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

TRAJECTORY_PATH = Path("/logs/agent/trajectory.json")
WORKERS_DIR = Path("/logs/agent/verification/workers")
MAIN_REPORT_PATH = Path("/logs/agent/harness_main.txt")
WORKER_REPORT_PATH = Path("/logs/agent/harness_workers.txt")
FAILURE_COUNTS_PATH = Path("/logs/agent/harness_failures.json")

# What a broken harness looks like in tool output. A line is classified under the first signature that
# claims it, so the order is most to least specific: `playwright: not found` is a missing browser
# before it is a missing command, and a missing browser cache is not also a missing path. The names
# are the keys in harness_failures.json and the reasons the judge is shown.
FAILURE_SIGNATURES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "unknown_skill",
        re.compile(r"Unknown skill:|No such skill|skill .{0,60}(?:could not be|was not) found", re.IGNORECASE),
    ),
    (
        "missing_plugin",
        re.compile(r"Unknown plugin|plugin .{0,60}(not found|not installed|unavailable)", re.IGNORECASE),
    ),
    (
        "missing_browser",
        # Playwright's own diagnostics, and nothing that merely mentions a browser by name. The earlier
        # `(?:playwright|chromium|chrome).{0,40}(?:not found|No such file)` arm swept up any absent path
        # whose name happened to contain one of those words -- `ls: cannot access 'chrome_profile': No
        # such file or directory` -- and because this signature sorts ahead of `missing_path`, it
        # *counted* the probe that `missing_path` exists to leave uncounted. Checking whether a browser
        # is installed before running is the careful thing to do; it must not read as breakage.
        # A bare `playwright install` is not a signature either: it is the remedy, and it appears far
        # more often in prose telling an agent not to run it than in any actual failure.
        re.compile(
            r"Executable doesn't exist|BrowserType\.launch:.{0,80}(not|missing)|"
            r"playwright(?:\.\S+)?: not found|ms-playwright.{0,40}(?:No such file|not found)",
            re.IGNORECASE,
        ),
    ),
    (
        "missing_module",
        # Python's three, then Node's: these evals commission browser apps, so a missing npm dependency
        # is as much a harness failure as a missing wheel, and was previously invisible.
        re.compile(
            r"ModuleNotFoundError|ImportError: cannot import|No module named|"
            r"Cannot find module|ERR_MODULE_NOT_FOUND|Cannot find package",
            re.IGNORECASE,
        ),
    ),
    # `Exit code 127` is a signature in its own right because a shell reports exactly that when a
    # command does not exist, and it survives the `2>/dev/null` that swallows the message. Without it
    # the criterion scores the agent's stderr handling rather than the workspace: two agents that hit
    # the same absent binary count differently depending on whether one redirected the complaint away.
    (
        "missing_command",
        # `Exit code 127` and `Exit code: 127` are the same fact in two renderings: the bare form is
        # what the trajectory records for a Bash step, the colon form is what mngr's own CLI prints.
        # The bare `not found` arm requires the shell's own prefix (`sh: 1: ss: not found`, which is
        # what dash prints where bash says "command not found"). Matching any `<token>: not found`
        # swept up ordinary key/value output -- `status: not found` -- as a missing binary.
        re.compile(
            r"command not found|Exit code:? 127\b|"
            r"(?:^|\s)(?:ba|z|k|da)?sh: (?:line )?(?:\d+: )?\S+: not found",
            re.IGNORECASE,
        ),
    ),
    # Anchored to the start of the line or a space: without it, prose and file contents match on the
    # `ls: ` inside words like `models: `, and observation text carries both.
    ("missing_path", re.compile(r"(?:^|\s)(?:ls|cat|cd|stat): .{0,120}No such file or directory", re.IGNORECASE)),
)

# Tools whose output is testimony about the harness, because they ran something. A `Read` result is a
# file's contents and an `Agent` result is another model's prose: both routinely quote the exact strings
# a broken harness prints -- a reference doc saying "do not `playwright install`", a review agent
# describing the browser tests -- so classifying them counts documentation *about* a failure as the
# failure. An errored result is scanned whatever tool produced it, since the error is the harness
# speaking rather than the content the agent asked for.
EXECUTING_TOOLS: frozenset[str] = frozenset({"Bash", "BashOutput"})


# Signatures that are evidence for the judge but are NOT counted against the scripted score. An agent
# checking whether a path exists and finding it absent is ordinary exploration, not a broken harness --
# and counting it means an agent that explores more scores as though its workspace were more broken.
# The judge is told the same thing in prompt.md; this is what makes the programmatic half agree.
UNCOUNTED_SIGNATURES: frozenset[str] = frozenset({"missing_path"})

# The failure count at which a scope scores half marks, recorded into the counts file so the scorer
# uses the number the counting was done under. harness_quality/checks.py owns the curve.
HALF_MARKS_AT = 4

# Enough of a message or a tool result to see what broke and whether the agent recovered, without any
# single step crowding the judge's context.
MESSAGE_CLIP = 1200
OBSERVATION_CLIP = 1500
ARGUMENT_CLIP = 600
# A hard ceiling on a report: rewardkit refuses a judge file over 1 MiB.
REPORT_CLIP = 400_000
# ...but a worker never gets less than this, however many workers share the budget. A report clipped
# below it says too little about what broke to be worth grading.
MIN_WORKER_REPORT_CLIP = 40_000


def _clip(text: str, limit: int) -> str:
    """``text`` cut to at most ``limit`` characters, saying how much it dropped.

    The notice counts against the limit rather than being appended past it: these budgets exist to keep
    a report under rewardkit's judge-file cap, and a clip that overshoots its own bound is not a bound.
    Room is reserved against the longest the notice could be, so the result is always within ``limit``.
    """
    text = text or ""
    if len(text) <= limit:
        return text
    notice = "\n[... {} more characters]"
    kept = limit - len(notice.format(len(text)))
    if kept <= 0:
        return text[:limit]
    return text[:kept] + notice.format(len(text) - kept)


def _results(step: dict[str, Any]) -> list[dict[str, Any]]:
    """Every tool result the step recorded, in order. The one place that knows the shape."""
    observation = step.get("observation")
    if not isinstance(observation, dict):
        return []
    results = observation.get("results")
    if not isinstance(results, list):
        return []
    return [result for result in results if isinstance(result, dict)]


def _is_errored_result(result: dict[str, Any]) -> bool:
    extra = result.get("extra")
    return isinstance(extra, dict) and bool(extra.get("is_error"))


def _observation_text(step: dict[str, Any]) -> str:
    return "\n".join(str(result.get("content") or "") for result in _results(step))


def _is_errored(step: dict[str, Any]) -> bool:
    return any(_is_errored_result(result) for result in _results(step))


def signatures_in(text: str) -> list[str]:
    """The failure signatures present in one blob of tool output, each named at most once. Per line,
    under the first signature that claims it -- see FAILURE_SIGNATURES for why the order is the rule."""
    found: list[str] = []
    for line in (text or "").splitlines():
        for name, pattern in FAILURE_SIGNATURES:
            if pattern.search(line):
                if name not in found:
                    found.append(name)
                break
    return found


def scored_total(counts: dict[str, int]) -> int:
    """The failure count the programmatic criteria score: every signature except the uncounted ones."""
    return sum(value for name, value in counts.items() if name not in UNCOUNTED_SIGNATURES)


def _tool_calls(step: dict[str, Any]) -> list[dict[str, Any]]:
    return [call for call in (step.get("tool_calls") or []) if isinstance(call, dict)]


def _failure_scan_text(step: dict[str, Any]) -> str:
    """The tool output eligible for signature classification: what an executing tool printed, plus
    every errored result. See EXECUTING_TOOLS for why the rest of a step's output is excluded."""
    tool_by_call_id = {call.get("tool_call_id"): call.get("function_name") for call in _tool_calls(step)}
    return "\n".join(
        str(result.get("content") or "")
        for result in _results(step)
        if _is_errored_result(result) or tool_by_call_id.get(result.get("source_call_id")) in EXECUTING_TOOLS
    )


def _skill_names(step: dict[str, Any]) -> list[str]:
    names = []
    for call in _tool_calls(step):
        if call.get("function_name") == "Skill":
            arguments = call.get("arguments")
            if isinstance(arguments, dict):
                names.append(str(arguments.get("skill") or "?"))
    return names


def _render_step(index: int, step: dict[str, Any], reasons: list[str]) -> str:
    """One step of a harness report: why it was kept, what the agent said, what it ran, what came back."""
    lines = ["--- step {} [{}]".format(index, ", ".join(reasons))]
    message = str(step.get("message") or "").strip()
    if message:
        lines.append("AGENT SAID: {}".format(_clip(message, MESSAGE_CLIP)))
    for call in _tool_calls(step):
        arguments = call.get("arguments")
        detail = ""
        if isinstance(arguments, dict):
            for key in ("skill", "command", "file_path", "prompt"):
                if arguments.get(key):
                    detail = _clip(str(arguments[key]), ARGUMENT_CLIP)
                    break
        lines.append("RAN {}: {}".format(call.get("function_name"), detail))
    observation = _observation_text(step)
    if observation:
        lines.append("GOT{}: {}".format(" (ERROR)" if _is_errored(step) else "", _clip(observation, OBSERVATION_CLIP)))
    return "\n".join(lines)


def render_harness_report(
    steps: list[dict[str, Any]], scope: str, report_clip: int = REPORT_CLIP
) -> tuple[str, dict[str, int]]:
    """The report for one trajectory, and its per-signature failure counts.

    A step is kept when it invoked a skill, when a tool result came back an error, or when any tool
    result matches a failure signature. Everything else is ordinary work the harness did not get in
    the way of.
    """
    count_by_signature: Counter[str] = Counter()
    blocks: list[str] = []
    for index, step in enumerate(steps, 1):
        found = signatures_in(_failure_scan_text(step))
        skills = _skill_names(step)
        is_errored = _is_errored(step)
        if not (found or skills or is_errored):
            continue
        reasons = []
        if skills:
            reasons.append("skill: {}".format(", ".join(skills)))
        if is_errored:
            reasons.append("tool error")
        reasons.extend(found)
        count_by_signature.update(found)
        blocks.append(_render_step(index, step, reasons))
    header = "{} -- {} steps, {} kept as harness-relevant".format(scope, len(steps), len(blocks))
    body = (
        "\n\n".join(blocks) if blocks else "(no skill invocation, tool error or failure signature in this trajectory)"
    )
    return _clip("{}\n{}\n\n{}\n".format(header, "=" * len(header), body), report_clip), count_by_signature


def load_steps(path: Path) -> list[dict[str, Any]]:
    """The ATIF steps at ``path``; empty when the file is absent or not a document."""
    try:
        document = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(document, dict):
        return []
    steps = document.get("steps")
    if not isinstance(steps, list):
        return []
    return [step for step in steps if isinstance(step, dict)]


def worker_trajectories(workers_dir: Path) -> list[tuple[str, Path]]:
    """(worker name, trajectory path) for every captured worker, in name order."""
    if not workers_dir.is_dir():
        return []
    found = []
    for child in sorted(workers_dir.iterdir()):
        trajectory = child / "trajectory.json"
        if child.is_dir() and trajectory.is_file():
            found.append((child.name, trajectory))
    return found


def _scope_record(count_by_signature: dict[str, int]) -> dict[str, Any]:
    """One scope's entry in the counts file: what was seen, and how much of it is scored. Both scopes
    go through here so the shape checks.py reads cannot drift apart between them."""
    return {
        "counts": count_by_signature,
        "total": sum(count_by_signature.values()),
        "scored_total": scored_total(count_by_signature),
    }


def write_reports(
    trajectory_path: Path,
    workers_dir: Path,
    main_report_path: Path,
    worker_report_path: Path,
    counts_path: Path,
) -> dict[str, Any]:
    """Write both reports and the counts file for one trial; returns what went into the counts file.

    Takes its paths rather than reading the module constants, the way finalize.py does, so the whole
    pass is exercisable outside the verifier container -- the writer and the reader of the counts
    contract are otherwise only ever tested apart, and a renamed key would pass both suites.
    """
    main_report, main_count_by_signature = render_harness_report(load_steps(trajectory_path), "LEAD AGENT")
    main_report_path.write_text(main_report)

    worker_reports: list[str] = []
    worker_count_by_signature: Counter[str] = Counter()
    workers = worker_trajectories(workers_dir)
    worker_budget = max(REPORT_CLIP // max(len(workers), 1), MIN_WORKER_REPORT_CLIP)
    for name, path in workers:
        report, count_by_signature = render_harness_report(load_steps(path), "WORKER {}".format(name), worker_budget)
        worker_reports.append(report)
        worker_count_by_signature.update(count_by_signature)
    if not workers:
        # A run that launched no worker is not a harness failure, and must not read to the judge as an
        # empty file it should punish. It has nothing to grade, and checks.py scores it accordingly.
        worker_reports.append("(this trial launched no worker, so there is no worker trajectory to grade)")
    # Per-worker budgets bound each report, but MIN_WORKER_REPORT_CLIP is a floor, so enough
    # workers still overrun the cap. rewardkit does not error on an oversized judge file -- it
    # hands the judge `[skipped: file too large]` -- which would leave the judge grading nothing
    # while the scripted criterion still counted every signature.
    worker_report_path.write_text(_clip("\n\n".join(worker_reports), REPORT_CLIP))

    recorded = {
        "main": _scope_record(main_count_by_signature),
        "workers": {**_scope_record(worker_count_by_signature), "worker_count": len(workers)},
        "half_marks_at": HALF_MARKS_AT,
        "uncounted_signatures": sorted(UNCOUNTED_SIGNATURES),
    }
    counts_path.write_text(json.dumps(recorded, indent=2))
    return recorded


def main() -> None:
    write_reports(
        trajectory_path=TRAJECTORY_PATH,
        workers_dir=WORKERS_DIR,
        main_report_path=MAIN_REPORT_PATH,
        worker_report_path=WORKER_REPORT_PATH,
        counts_path=FAILURE_COUNTS_PATH,
    )


if __name__ == "__main__":
    main()
