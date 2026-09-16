"""Render the expectations the outcome judge grades against, at GRADE time, from the case config.

The judge needs the case's ground truth in prose: what was commissioned, and which concrete checks
the harness recorded against it. Rendering here (a verifier pre-step, before rewardkit) rather than
in the driver means ``harbor trial regrade`` re-scores captured trials under the current rendering.

Runs in the verifier container: stdlib only, absolute paths. Cases with no expectations have no
tests/outcome/ directory, so test.sh never invokes this.
"""

import json
from pathlib import Path
from typing import Any

CASE_PATH = Path("/tests/case.json")
EXPECTATIONS_PATH = Path("/logs/agent/expectations.md")


def _declared_actions(check: dict[str, Any]) -> str:
    """What the flow declares it does. Read as `actions`, with `steps` as the name a dataset
    generated before the rename carries; a regrade of such a trial still has to show the judge the
    flow it ran.

    CLEANUP: drop the `steps` fallback once no trial generated before 2026-09-11 is regraded any
    more (after the next minds release ships with the rename).
    """
    return str(check.get("actions") or check.get("steps") or "")


def _process_line(check: dict[str, Any]) -> str:
    """One process check as a line the judge reads: what the case asked of the way the agent worked.

    Stated as an expectation rather than as a verdict -- the manifest carries whether it held, and
    the judge weighs that itself.
    """
    kind = str(check.get("kind") or "")
    if kind == "required_skill":
        return "- The `{}` skill was to be invoked.".format(check.get("skill"))
    elif kind == "forbidden_skill":
        return "- The `{}` skill was not to be invoked.".format(check.get("skill"))
    elif kind == "max_worker_launches":
        return "- At most {} background worker(s) were to be launched.".format(check.get("max_worker_launches"))
    else:
        return "- (a `{}` process check, which this renderer cannot describe)".format(kind)


def _seconds_text(raw_value: Any) -> str:
    """An anchor as the judge should read it. Anchors are authored as whole seconds and travel as
    floats, so the trailing `.0` is dropped; anything this cannot read is printed as it came, since
    a renderer that raised would fail the grade before a criterion ran."""
    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
        return str(raw_value)
    return str(int(raw_value)) if float(raw_value).is_integer() else str(raw_value)


def _timing_line(check: dict[str, Any]) -> str:
    """One timing check as a line the judge reads: how long the case allowed, and what the time was
    conditional on.

    Stated as an expectation rather than as a verdict, like every other class: the manifest entry
    carries the time actually measured and why it counted or did not.
    """
    prerequisites = [str(name) for name in (check.get("requires_no_failures") or [])]
    conditional_clause = (
        " -- and the time only counts if nothing failed in: {}".format(
            ", ".join("`{}`".format(name) for name in prerequisites)
        )
        if prerequisites
        else ""
    )
    return "- The client's goal was to be met within {} second(s), and no later than {} second(s){}.".format(
        _seconds_text(check.get("fast_seconds")), _seconds_text(check.get("slow_seconds")), conditional_clause
    )


def render_expectations(case: dict[str, Any]) -> str:
    """The judge-facing markdown for one case: the outcome prose plus every declared check."""
    expectations = case.get("expectations") or {}
    lines: list[str] = [
        "# Expected outcome for `{}`".format(case.get("case_id") or "unknown"),
        "",
        str(expectations.get("outcome") or "(no outcome prose was declared)"),
        "",
    ]

    app_checks = expectations.get("app_checks") or []
    if app_checks:
        lines += ["## Delivered apps", ""]
        for check in app_checks:
            service_clause = ", each with a running service" if check.get("is_supervisord_service_required") else ""
            lines.append(
                "- At least {} app(s) registered in the workspace app registry{}.".format(
                    check.get("min_registered_apps"), service_clause
                )
            )
        lines.append("")

    http_checks = expectations.get("http_checks") or []
    if http_checks:
        lines += ["## HTTP probes", ""]
        for check in http_checks:
            body_regex = check.get("expect_body_regex") or ""
            body_clause = ", with a body matching `{}`".format(body_regex) if body_regex else ""
            lines.append(
                "- `{}` must answer HTTP {}{}.".format(check.get("target"), check.get("expect_status"), body_clause)
            )
        lines.append("")

    files_checks = expectations.get("files_checks") or []
    if files_checks:
        lines += ["## Delivered files", ""]
        for check in files_checks:
            lines.append("- At least {} file(s) matching `{}`.".format(check.get("min_count"), check.get("glob")))
        lines.append("")

    ui_flow_checks = expectations.get("ui_flow_checks") or []
    if ui_flow_checks:
        lines += ["## UI flows", ""]
        for check in ui_flow_checks:
            lines.append("- **{}**".format(check.get("name")))
            lines.append("  - Actions: {}".format(_declared_actions(check)))
            lines.append("  - Expected end state: {}".format(check.get("expect")))
        lines.append("")

    process_checks = expectations.get("process_checks") or []
    if process_checks:
        lines += ["## How the work was to be done", ""]
        for check in process_checks:
            lines.append(_process_line(check))
        lines.append("")

    timing_checks = expectations.get("timing_checks") or []
    if timing_checks:
        lines += ["## How quickly the work was to be done", ""]
        for check in timing_checks:
            lines.append(_timing_line(check))
        lines.append("")

    test_commands = expectations.get("test_commands") or []
    if test_commands:
        lines += ["## Recorded test commands (never gated)", ""]
        for command in test_commands:
            lines.append("- `{}`".format(command))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    try:
        case = json.loads(CASE_PATH.read_text())
    except (OSError, ValueError):
        case = {}
    EXPECTATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    EXPECTATIONS_PATH.write_text(render_expectations(case if isinstance(case, dict) else {}))


if __name__ == "__main__":
    main()
