"""Keep the judges' derived inputs where the trial's job directory can see them.

The grade-time pre-steps write what the judges read under `/logs/agent`, inside this container, and
the job directory never receives that copy. harbor collects `/logs/verifier` beside
`reward-details.json`, so the inputs a reader needs to tell what the verifier derived -- the
judged transcript with its progress blocks, the progress summary, the failure-signature counts, the
flow digest, and which screenshots were attached -- are copied under it.

test.sh runs this on its way out whatever happened before, so an input that was never written is
skipped, and nothing here may fail the grade: every copy is attempted on its own.

Runs in the verifier container: stdlib only, absolute paths.
"""

import shutil
import sys
from pathlib import Path

AGENT_LOGS_DIR = Path("/logs/agent")
DERIVED_DIR = Path("/logs/verifier/derived")

DERIVED_FILENAMES = (
    "judge_transcript.txt",
    "progress_summary.json",
    "harness_failures.json",
    "judge_flows_digest.txt",
)
SCREENSHOTS_DIRNAME = "judge_screenshots"
# The attached screenshots are listed by name rather than copied: the flow evidence the trial
# collected already holds every frame, and the names are what say which of them the judge saw.
SCREENSHOT_NAMES_FILENAME = "judge_screenshots.txt"


def keep_derived_outputs(agent_logs_dir: Path, derived_dir: Path) -> list[str]:
    """Copy every derived input that exists into `derived_dir`; returns what could not be kept."""
    failures: list[str] = []
    try:
        derived_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return ["{}: {}".format(derived_dir, exc)]
    for filename in DERIVED_FILENAMES:
        source = agent_logs_dir / filename
        if not source.is_file():
            continue
        try:
            shutil.copyfile(source, derived_dir / filename)
        except OSError as exc:
            failures.append("{}: {}".format(source, exc))
    screenshots_dir = agent_logs_dir / SCREENSHOTS_DIRNAME
    if screenshots_dir.is_dir():
        try:
            names = sorted(path.name for path in screenshots_dir.iterdir() if path.is_file())
            (derived_dir / SCREENSHOT_NAMES_FILENAME).write_text("".join(name + "\n" for name in names))
        except OSError as exc:
            failures.append("{}: {}".format(screenshots_dir, exc))
    return failures


def main() -> None:
    for failure in keep_derived_outputs(AGENT_LOGS_DIR, DERIVED_DIR):
        print("keep_derived_outputs: could not keep {}".format(failure), file=sys.stderr)


if __name__ == "__main__":
    main()
