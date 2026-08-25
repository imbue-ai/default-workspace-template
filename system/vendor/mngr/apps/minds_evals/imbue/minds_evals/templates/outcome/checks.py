"""Programmatic outcome criteria: score the evidence the driver recorded while the workspace was
alive, never live state (by grade time the workspace is long destroyed).

One criterion per lowered expectation class, and only for the classes this case actually declares --
an absent class contributes nothing in either direction rather than a silent zero. Entries the
collector marked ``error`` (the harness could not find out) are excluded from the denominator; when
a declared class has no determinable entry at all, this scores 0.0 and finalize.py turns that into a
grading-infrastructure failure, so the agent is never charged for a broken instrument.

Runs in the verifier container: stdlib + rewardkit only. The absolute paths are harbor's verifier
contract rather than this harness's choice: ``/tests`` is where the task's tests directory lands in
the container, and ``/logs/agent/...`` is where the task's declared artifacts are re-materialized at
their original absolute paths.
"""

import fnmatch
import json
from pathlib import Path
from typing import Any

import rewardkit
from rewardkit import criterion

CASE_PATH = Path("/tests/case.json")
MANIFEST_PATH = Path("/logs/agent/verification/manifest.json")
INVENTORY_PATH = Path("/logs/agent/verification/file_inventory.jsonl")

FILES_CLASS = "files"
APP_CLASS = "app"
HTTP_CLASS = "http"

# Which lowered check list makes a class scored, and what its criterion is called. Classes absent
# from the case (or reserved for a later phase, like ui_flows) register no criterion at all.
CRITERION_BY_CLASS = (
    (FILES_CLASS, "files_checks", "files_expectations_met"),
    (APP_CLASS, "app_checks", "app_registered"),
    (HTTP_CLASS, "http_checks", "http_expectations_met"),
)


# Every helper below degrades to an empty result instead of raising, by design: a criterion that
# raises aborts rewardkit's whole run with no reward file for ANY dimension, which would grade a
# broken instrument as a broken trial. A missing or malformed input therefore scores 0.0 here and
# is diagnosed by finalize.py, which re-reads the manifest itself and turns "no determinable
# evidence" into a grading-infrastructure failure. Do not add raises to this file.
def _load_json(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _lowered_expectations() -> dict[str, Any]:
    expectations = _load_json(CASE_PATH).get("expectations")
    return expectations if isinstance(expectations, dict) else {}


def _manifest_entries(check_class: str) -> list[dict[str, Any]]:
    entries = _load_json(MANIFEST_PATH).get("entries")
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict) and entry.get("check_class") == check_class]


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
    """The fraction of a class's determinable recorded checks that the workspace passed."""
    determinable = [entry for entry in _manifest_entries(check_class) if entry.get("status") != "error"]
    if not determinable:
        return 0.0
    passed = sum(1 for entry in determinable if entry.get("status") == "passed")
    return passed / len(determinable)


def _files_score() -> float:
    """Declared globs are matched against the captured inventory here rather than at collection
    time, so a regrade picks up a corrected glob without re-running the trial."""
    checks = _lowered_expectations().get("files_checks") or []
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


@criterion(description="Recorded outcome checks of one expectation class that the delivered workspace met")
def expectation_class_met(workspace: Path, check_class: str) -> float:
    """Score one expectation class from the recorded evidence, never from live state."""
    try:
        if check_class == FILES_CLASS:
            return _files_score()
        return _recorded_class_score(check_class)
    # rewardkit aborts the whole grade (every dimension, no reward file) when a criterion raises, so
    # a malformed check must degrade to a zero here and be diagnosed by finalize.py instead.
    except (TypeError, ValueError):
        return 0.0


# Registration goes through the rewardkit module rather than the decorated name: @criterion returns
# a handle that refuses a direct call, and only the module-level lookup reaches the factory that
# actually registers a check (and accepts the criterion's name).
for _check_class, _lowered_key, _criterion_name in CRITERION_BY_CLASS:
    if _lowered_expectations().get(_lowered_key):
        rewardkit.expectation_class_met(_check_class, name=_criterion_name)
