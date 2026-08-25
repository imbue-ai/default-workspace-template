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


def render_expectations(case: dict[str, Any]) -> str:
    """The judge-facing markdown for one case: the outcome prose plus every declared check."""
    lowered = case.get("expectations") or {}
    lines: list[str] = [
        "# Expected outcome for `{}`".format(case.get("case_id") or "unknown"),
        "",
        str(lowered.get("outcome") or "(no outcome prose was declared)"),
        "",
    ]

    app_checks = lowered.get("app_checks") or []
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

    http_checks = lowered.get("http_checks") or []
    if http_checks:
        lines += ["## HTTP probes", ""]
        for check in http_checks:
            body_regex = check.get("expect_body_regex") or ""
            body_clause = ", with a body matching `{}`".format(body_regex) if body_regex else ""
            lines.append(
                "- `{}` must answer HTTP {}{}.".format(check.get("target"), check.get("expect_status"), body_clause)
            )
        lines.append("")

    files_checks = lowered.get("files_checks") or []
    if files_checks:
        lines += ["## Delivered files", ""]
        for check in files_checks:
            lines.append("- At least {} file(s) matching `{}`.".format(check.get("min_count"), check.get("glob")))
        lines.append("")

    test_commands = lowered.get("test_commands") or []
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
