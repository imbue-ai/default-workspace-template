"""Programmatic outcome criteria: score the evidence the driver recorded while the workspace was
alive, never live state (by grade time the workspace is long destroyed).

One criterion per expanded expectation class, and only for the classes this case actually declares --
an absent class contributes nothing in either direction rather than a silent zero. Entries the
collector marked ``error`` (the harness could not find out) are excluded from the denominator; when
a declared class has no determinable entry at all, this scores 0.0 and finalize.py turns that into a
grading-infrastructure failure, so the agent is never charged for a broken instrument.

Most classes are a fraction of pass/fail checks. ``timing`` is not: it scores one continuous
measurement the collector recorded on a curve, and its unmeasurable state -- a client that never
said its goal was met -- is a legitimate 0.0 rather than a grading failure, so finalize.py does not
carry it.

Runs in the verifier container: stdlib + rewardkit only. The absolute paths are harbor's verifier
contract rather than this harness's choice: ``/tests`` is where the task's tests directory lands in
the container, and ``/logs/agent/...`` is where the task's declared artifacts are re-materialized at
their original absolute paths.
"""

import fnmatch
import json
import math
from pathlib import Path
from typing import Any

import rewardkit as rk
from rewardkit import criterion

CASE_PATH = Path("/tests/case.json")
MANIFEST_PATH = Path("/logs/agent/verification/manifest.json")
INVENTORY_PATH = Path("/logs/agent/verification/file_inventory.jsonl")

FILES_CLASS = "files"
APP_CLASS = "app"
HTTP_CLASS = "http"
UI_FLOWS_CLASS = "ui_flows"
PROCESS_CLASS = "process"
TIMING_CLASS = "timing"

# Which expanded check list makes a class scored, and what its criterion is called. A class the case
# does not declare registers no criterion at all, so it contributes nothing in either direction.
CRITERION_BY_CLASS = (
    (FILES_CLASS, "files_checks", "files_expectations_met"),
    (APP_CLASS, "app_checks", "app_registered"),
    (HTTP_CLASS, "http_checks", "http_expectations_met"),
    (UI_FLOWS_CLASS, "ui_flow_checks", "ui_flows_completed"),
    (PROCESS_CLASS, "process_checks", "process_expectations_met"),
    (TIMING_CLASS, "timing_checks", "time_to_goal_within_expectations"),
)


# Every helper below degrades to an empty result instead of raising, by design: a criterion that
# raises aborts rewardkit's whole run with no reward file for ANY dimension, which would grade a
# broken instrument as a broken trial. A missing or malformed input therefore scores 0.0 here (or,
# for case.json, registers no criteria at all) and is diagnosed by finalize.py, which re-reads both
# files itself and turns "no determinable evidence" and "no trustworthy case file" alike into a
# grading-infrastructure failure. Do not add raises to this file.
def _load_json(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _expectations() -> dict[str, Any]:
    expectations = _load_json(CASE_PATH).get("expectations")
    return expectations if isinstance(expectations, dict) else {}


def _all_manifest_entries() -> list[dict[str, Any]]:
    entries = _load_json(MANIFEST_PATH).get("entries")
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def _manifest_entries(check_class: str) -> list[dict[str, Any]]:
    return [entry for entry in _all_manifest_entries() if entry.get("check_class") == check_class]


def _inventory_paths() -> list[str]:
    try:
        lines = INVENTORY_PATH.read_text().splitlines()
    except OSError:
        return []
    paths: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("path"):
            paths.append(str(record["path"]))
    return paths


def _recorded_class_score(check_class: str) -> float:
    """The fraction of a class's determinable recorded checks that the workspace passed.

    For ``ui_flows`` a passing entry means the flow COMPLETED -- its declared steps were carried out
    -- not that the app did what the flow's ``expect`` describes. Whether the expectation holds is
    the outcome judge's ruling, made from the step log and the screenshots; scoring it here as well
    would be a second verdict on the same question, taken from less evidence.
    """
    determinable = [entry for entry in _manifest_entries(check_class) if entry.get("status") != "error"]
    if not determinable:
        return 0.0
    passed = sum(1 for entry in determinable if entry.get("status") == "passed")
    return passed / len(determinable)


def _files_score() -> float:
    """Declared globs are matched against the captured inventory here rather than at collection
    time, so a regrade picks up a corrected glob without re-running the trial."""
    checks = _expectations().get("files_checks") or []
    if not checks:
        return 0.0
    # An inventory that could not be captured is recorded as an error entry, which finalize.py reads
    # as a grading-infrastructure failure; scoring zero here keeps that path from being masked.
    if any(entry.get("status") == "error" for entry in _manifest_entries(FILES_CLASS)):
        return 0.0
    paths = _inventory_paths()
    met = 0
    for check in checks:
        glob = str(check.get("glob") or "")
        min_count = int(check.get("min_count") or 1)
        # fnmatchcase, not fnmatch: the inventory holds POSIX paths from the workspace, and matching
        # them must not depend on the case sensitivity of whatever host the verifier runs on.
        if sum(1 for path in paths if fnmatch.fnmatchcase(path, glob)) >= min_count:
            met += 1
    return met / len(checks)


def _failed_check_classes(entries: list[dict[str, Any]]) -> set[str]:
    """Which classes recorded a check the workspace fell short on, for a timing prerequisite to read.

    Only ``failed`` counts: an ``error`` is the harness not finding out, and holding an unmeasured
    class against the clock would charge the agent for a broken instrument.
    """
    return {str(entry.get("check_class")) for entry in entries if entry.get("status") == "failed"}


def timing_score(seconds: float | None, fast_seconds: float, slow_seconds: float) -> float:
    """The timing curve: clamped, and linear in the logarithm of the measured time.

    1.0 at or under the fast anchor, 0.0 at or over the slow one, and a log-linear ramp between, so
    the curve is scale-free -- halving a slow trial's time is worth as much as halving a fast one's,
    which is what makes the per-case anchors comparable across cases.

    ``seconds`` is None when nothing measured the time, which for this class means the client was
    never satisfied: unboundedly slow, and so 0.0 rather than an error. Anchors that cannot define a
    curve score 0.0 too, because a criterion here must never raise.
    """
    if seconds is None or fast_seconds <= 0.0 or slow_seconds <= fast_seconds:
        return 0.0
    if seconds <= fast_seconds:
        return 1.0
    if seconds >= slow_seconds:
        return 0.0
    return (math.log(slow_seconds) - math.log(seconds)) / (math.log(slow_seconds) - math.log(fast_seconds))


def _measured_seconds(raw_value: Any) -> float | None:
    """The measurement an entry carries, or None when it carries none. A bool is excluded because
    isinstance(True, int) holds, and a manifest that somehow held one is not a measured time."""
    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
        return None
    return float(raw_value)


def timing_class_score(checks: list[dict[str, Any]], entries: list[dict[str, Any]]) -> float:
    """How fast the agent got the goal-holding client to say its goal was met.

    The seconds are the collector's measurement, read out of the entry's ``value`` rather than
    recomputed here, so collection and grading cannot disagree about the number. This applies the
    curve, and the prerequisite: a class the case named in ``requires_no_failures`` that recorded a
    failed check zeroes the time outright, since being fast at something other than what the case
    commissioned is not what the class is for.
    """
    if not checks:
        return 0.0
    value_by_entry_id = {
        str(entry.get("entry_id")): entry.get("value") for entry in entries if entry.get("check_class") == TIMING_CLASS
    }
    failed_classes = _failed_check_classes(entries)
    scores: list[float] = []
    for check in checks:
        prerequisites = check.get("requires_no_failures") or []
        if any(str(name) in failed_classes for name in prerequisites):
            scores.append(0.0)
            continue
        scores.append(
            timing_score(
                _measured_seconds(value_by_entry_id.get(str(check.get("check_id")))),
                float(check.get("fast_seconds") or 0.0),
                float(check.get("slow_seconds") or 0.0),
            )
        )
    return sum(scores) / len(scores)


@criterion(description="Recorded outcome checks of one expectation class that the delivered workspace met")
def expectation_class_met(workspace: Path, check_class: str) -> float:
    """Score one expectation class from the recorded evidence, never from live state."""
    try:
        if check_class == FILES_CLASS:
            return _files_score()
        if check_class == TIMING_CLASS:
            return timing_class_score(_expectations().get("timing_checks") or [], _all_manifest_entries())
        return _recorded_class_score(check_class)
    # rewardkit aborts the whole grade (every dimension, no reward file) when a criterion raises, so
    # a malformed check must degrade to a zero here and be diagnosed by finalize.py instead.
    except (TypeError, ValueError):
        return 0.0


def _is_class_scorable(check_class: str, expectation_key: str) -> bool:
    """Whether this case's evidence supports scoring one class at all.

    Declaring the class is the first condition. For UI flows there is a second: at least one
    determinable entry. A flow is driven by a browser against a serving proxy, so "the browser
    never launched", "the proxy never served", "the tunnel is down" all yield a class where every
    entry is an error -- and scoring that would charge the agent for machinery it did not break,
    while erroring the trial (what finalize.py does for the other classes) would throw away a
    perfectly good conversation-quality measurement over the same broken machinery. Registering
    nothing leaves the flows out of the score in either direction, which is what an unmeasurable
    check should cost. The manifest still records which part broke, and the judge still sees it.

    The cheap classes keep the stricter rule: an inventory, a registry or a transcript that could
    not be read at all means the collection phase itself failed, which is worth erroring the trial
    over. ``timing`` needs no second condition at all: its entry is never an error, and a time that
    could not be measured is a client that was never satisfied, which scores zero on its own terms.
    """
    if not _expectations().get(expectation_key):
        return False
    if check_class != UI_FLOWS_CLASS:
        return True
    return any(entry.get("status") != "error" for entry in _manifest_entries(check_class))


# Registration goes through the rewardkit module rather than the decorated name: @criterion returns
# a handle that refuses a direct call, and only the module-level lookup reaches the factory that
# actually registers a check (and accepts the criterion's name).
#
# Registering NOTHING is a supported state, not a fault: expectations that commission no deliverable
# and declare no UI flows have no probeable class at all, and their outcome dimension is the judge
# alone. rewardkit warns that the criterion above was defined and never called; the dimension still
# scores.
for _check_class, _expectation_key, _criterion_name in CRITERION_BY_CLASS:
    if _is_class_scorable(_check_class, _expectation_key):
        rk.expectation_class_met(_check_class, name=_criterion_name)
