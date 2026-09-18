#!/usr/bin/env python3
"""Merge the junit files of repeated pytest passes into one offload-shaped junit.xml.

A suite that does not run under offload has no retries, so nothing records that a
test failed once and passed the next time. Running the whole suite several times
gives the same signal: a test that failed in some passes and passed in others is
flaky, and one that failed in every pass is broken.

Offload writes one `<testcase>` per attempt, named by the test's node id, and
`junit_test_summary.py` (and the flake sweep that reads its output) aggregates on
that name. pytest names a testcase by its bare function name instead, which
collides across files, so each testcase is renamed to its repo-relative node id
on the way through. The merged file is then indistinguishable from an offload
run in which every test was attempted once per pass.

Exits 1 when some test never passed, so the calling step fails exactly when a
retrying runner would have: a flaky test leaves the step green and the junit
carrying its failed attempts, which the flaky-aware report reads as "recovered".
"""

import argparse
import copy
import sys
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from pathlib import Path

from junit_test_summary import RunStatus
from junit_test_summary import parse_junit


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pass-junit", required=True, nargs="+", type=Path, help="The junit file of each pytest session."
    )
    parser.add_argument(
        "--path-prefix",
        required=True,
        help="Repo-relative directory pytest ran from, prepended to each testcase's file to form its node id.",
    )
    parser.add_argument("--output", required=True, type=Path, help="Where to write the merged junit.xml.")
    args = parser.parse_args(argv)

    missing_paths = [path for path in args.pass_junit if not path.is_file()]
    if missing_paths:
        print(f"junit file(s) not found: {', '.join(str(path) for path in missing_paths)}", file=sys.stderr)
        return 1

    merged = merge_passes(args.pass_junit, args.path_prefix)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(merged).write(args.output, encoding="utf-8", xml_declaration=True)

    per_test, _failures = parse_junit(args.output)
    never_passed = [
        record.name for record in per_test.values() if record.final_status in (RunStatus.FAILED, RunStatus.ERROR)
    ]
    for name in never_passed:
        print(f"never passed: {name}", file=sys.stderr)
    return 1 if never_passed else 0


def merge_passes(pass_junit_paths: Sequence[Path], path_prefix: str) -> ET.Element:
    """One `<testsuite>` per input file, every testcase renamed to its repo-relative node id."""
    merged = ET.Element("testsuites", {"name": "repeated-passes"})
    for junit_path in pass_junit_paths:
        suite = ET.SubElement(merged, "testsuite", {"name": junit_path.stem})
        for testcase in ET.parse(junit_path).iter("testcase"):
            renamed = copy.deepcopy(testcase)
            renamed.set("name", node_id_for(testcase, path_prefix))
            suite.append(renamed)
    return merged


def node_id_for(testcase: ET.Element, path_prefix: str) -> str:
    """The node id offload would have used: `<repo-relative file>::<test name>`.

    pytest writes the `file` attribute only under `junit_family = xunit1`. A testcase without it is
    a collection error or a family nobody configured, and its dotted classname is the best path left.
    """
    name = testcase.get("name") or ""
    file_attribute = testcase.get("file")
    if file_attribute is None:
        file_attribute = (testcase.get("classname") or "").replace(".", "/") + ".py"
    return f"{path_prefix.rstrip('/')}/{file_attribute}::{name}"


if __name__ == "__main__":
    sys.exit(main())
