"""Flatten the UI-flow evidence into the two shapes rewardkit's judge can actually read.

rewardkit inlines a judge's `files` list with no recursion: a listed directory contributes its
immediate FILES only, sorted by path. The collected evidence is nested one level deeper
(`verification/flows/<name>/step_003.png`), so a pre-step has to flatten it. Running at grade time
rather than in the driver is what lets `harbor trial regrade` re-score captured trials under a
changed selection or digest format.

Two rules drive the layout, both from how rewardkit renders a `files` entry:

- A listed path that does not exist renders a literal ``[not found]`` block into the judge's prompt.
  Both outputs here are therefore created unconditionally -- an oracle run or a case with no flows
  gets an empty screenshots directory and a digest that says so, never a missing-file marker.
- A listed directory that is EMPTY renders nothing at all, not even a header. That is why the digest
  is a separate always-present file and states the screenshot count explicitly: without it the judge
  could not tell "flows ran but captured no usable shot" from "no flows were declared".

Runs in the verifier container: stdlib only, absolute paths.
"""

import json
import shutil
import sys
from pathlib import Path
from typing import Any

VERIFICATION_DIR = Path("/logs/agent/verification")
# The case as the generator lowered it, which is where a flow's declared steps and its `expect`
# live. Same path outcome/checks.py reads: harbor mounts the task's tests directory there.
CASE_PATH = Path("/tests/case.json")
DIGEST_PATH = Path("/logs/agent/judge_flows_digest.txt")
SCREENSHOTS_DIR = Path("/logs/agent/judge_screenshots")

MANIFEST_FILENAME = "manifest.json"
READING_ACTION = "read the final state"
FLOWS_DIRNAME = "flows"
FLOW_LOG_FILENAME = "log.jsonl"
UI_FLOWS_CLASS = "ui_flows"

# rewardkit replaces any judge file over 1 MiB with a "[skipped: file too large]" marker, so an
# oversized shot buys noise instead of evidence.
MAX_SCREENSHOT_BYTES = 1024 * 1024
# What every flow is guaranteed: its final frame -- the state its `expect` is judged against -- and
# the three before it, which is what shows how the flow arrived there. Per flow rather than shared,
# so a long flow cannot leave a later one with no picture at all.
MAX_SCREENSHOTS_PER_FLOW = 4
# The safety valve on a case with many flows. Every shot is inlined as a base64 vision block and
# rewardkit imposes no total-size limit of its own, so something has to bound the request; this is
# high enough that the per-flow guarantee is what normally decides the selection.
MAX_SCREENSHOTS_TOTAL = 24
# The digest is inlined as text, and rewardkit drops a file over 1 MiB outright -- which would lose
# the whole flow record rather than part of it. Cut well below the line.
MAX_DIGEST_BYTES = 900_000
# How much of one step's page state is worth carrying. Generous, because the ARIA tree is what the
# judge reads instead of paying for vision on every frame.
MAX_STEP_STATE_CHARS = 16_000
# What earlier steps shrink to, in order, when the whole digest will not fit. A flow's LAST step is
# never rendered below MAX_STEP_STATE_CHARS while any earlier step still has room to give up: the
# final state is the one the `expect` is judged against.
_EARLIER_STEP_STATE_LADDER = (MAX_STEP_STATE_CHARS, 8_000, 4_000, 2_000, 1_000, 400)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


# How a manifest status reads in the digest. Trial time records whether a flow ran to the end of its
# declared steps; it does not rule on the `expect`, so the words here must not suggest it did.
_COMPLETION_BY_STATUS = {"passed": "completed", "failed": "incomplete", "error": "not measured"}


def _declared_flows(case_path: Path) -> dict[str, dict[str, Any]]:
    """Each declared flow by its check id: what it asked for, and what it expects to end up seeing.

    The judge cannot rule on an `expect` it was never shown, and the manifest carries only the
    recorded outcome, so this half comes from the case itself.
    """
    expectations = _load_json(case_path).get("expectations")
    checks = expectations.get("ui_flow_checks") if isinstance(expectations, dict) else None
    if not isinstance(checks, list):
        return {}
    return {
        str(check.get("check_id") or ""): check
        for check in checks
        if isinstance(check, dict) and check.get("check_id")
    }


def _flow_entries(verification_dir: Path) -> list[dict[str, Any]]:
    entries = _load_json(verification_dir / MANIFEST_FILENAME).get("entries")
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict) and entry.get("check_class") == UI_FLOWS_CLASS]


def _flow_directories(verification_dir: Path) -> list[Path]:
    """The per-flow evidence directories, in a stable order.

    Read from the filesystem rather than from the manifest so a flow whose entry never got written
    (a collector that died mid-phase) still contributes whatever steps it managed to record.
    """
    try:
        return sorted(path for path in (verification_dir / FLOWS_DIRNAME).iterdir() if path.is_dir())
    except OSError:
        return []


def _read_steps(flow_dir: Path) -> list[dict[str, Any]]:
    try:
        lines = (flow_dir / FLOW_LOG_FILENAME).read_text().splitlines()
    except OSError:
        return []
    steps: list[dict[str, Any]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(record, dict):
            steps.append(record)
    return steps


def _screenshots(flow_dir: Path) -> list[Path]:
    """A flow's step screenshots in step order. Names are zero-padded at capture time, which is
    what makes a plain sort chronological (and keeps rewardkit's own path sort chronological too)."""
    try:
        return sorted(path for path in flow_dir.iterdir() if path.suffix == ".png" and path.is_file())
    except OSError:
        return []


def _is_within_size(path: Path, max_bytes: int) -> bool:
    try:
        return path.stat().st_size <= max_bytes
    except OSError:
        return False


def select_screenshots(
    screenshots_by_flow: list[list[Path]], per_flow: int, total: int, max_bytes: int
) -> tuple[list[Path], int]:
    """The last `per_flow` usable shots of each flow, capped overall at `total`.

    The last shot of a flow is the state its `expect` is judged against, so it is the single most
    informative frame; taking it and the ones just before it keeps the frames that show how the
    flow ended. Every flow gets its own allowance, so a case with several flows cannot end up
    illustrating only the first. The overall cap is a safety valve, and it takes from the earliest
    flows first, leaving the later ones whole.

    Also reports how many shots the per-flow rule chose before that cap applied, so the digest can
    tell the judge when it is looking at a subset.
    """
    usable_by_flow = [
        [path for path in shots if _is_within_size(path, max_bytes)][-per_flow:] for shots in screenshots_by_flow
    ]
    chosen_count = sum(len(shots) for shots in usable_by_flow)
    selected: list[Path] = [path for shots in usable_by_flow for path in shots]
    if chosen_count > total:
        selected = selected[chosen_count - total :]
    # Chronological within each flow, flows in their collection order: the judge reads a sequence,
    # not a ranking.
    return selected, chosen_count


def _render_step(step: dict[str, Any], max_state_chars: int) -> list[str]:
    lines = [
        "  step {}: {}".format(step.get("step_index"), step.get("action") or "(no action)"),
        "    agent reasoning: {}".format(step.get("reasoning") or "(none recorded)"),
    ]
    state = str(step.get("state") or "")
    if len(state) > max_state_chars:
        state = state[:max_state_chars] + "\n[...page state truncated...]"
    error = str(step.get("error") or "")
    if error:
        # Without this the judge would read "click the button named 'Delete'" followed by an
        # unchanged screenshot and conclude the app ignored the click.
        lines.append("    THIS ACTION DID NOT RUN: {}".format(error))
    lines.append("    page state:")
    lines.extend("      " + line for line in (state.splitlines() or ["(no state captured)"]))
    return lines


def _flow_header(entry: dict[str, Any], check: dict[str, Any], steps: list[dict[str, Any]]) -> list[str]:
    """What the flow asked for, what it expects, how far it got, and what the agent says it saw.

    The `expect` is presented as the open question it is: the judge rules on it from the evidence
    below, and the agent's reading is one more piece of that evidence rather than an answer.
    """
    reading = next(
        (str(step.get("reasoning") or "") for step in reversed(steps) if step.get("action") == READING_ACTION),
        "",
    )
    return [
        "declared steps: {}".format(check.get("steps") or "(not recorded)"),
        "expect (YOU decide whether this holds): {}".format(check.get("expect") or "(not recorded)"),
        "completion: {} ({})".format(
            _COMPLETION_BY_STATUS.get(str(entry.get("status") or ""), "unknown"), entry.get("reason") or "-"
        ),
        "the agent's reading of the final state, as evidence rather than a verdict: {}".format(
            reading or "(none recorded)"
        ),
        "",
    ]


def _render_detail(
    flow_dirs: list[Path],
    entry_by_flow: dict[str, dict[str, Any]],
    steps_by_flow: dict[str, list[dict[str, Any]]],
    check_by_id: dict[str, dict[str, Any]],
    earlier_state_chars: int,
) -> str:
    """Every flow's header and steps, with each flow's LAST step rendered at the full per-step cap.

    The final state is the one a flow's `expect` is judged against, so it is the last thing that
    should give up room when the digest has to shrink.
    """
    detail_lines: list[str] = []
    for flow_dir in flow_dirs:
        entry = entry_by_flow.get(flow_dir.name, {})
        check = check_by_id.get(str(entry.get("entry_id") or ""), {})
        steps = steps_by_flow.get(flow_dir.name, [])
        detail_lines += ["## flow: {}".format(flow_dir.name), ""]
        detail_lines += _flow_header(entry, check, steps)
        for index, step in enumerate(steps):
            is_last = index == len(steps) - 1
            detail_lines.extend(_render_step(step, MAX_STEP_STATE_CHARS if is_last else earlier_state_chars))
        detail_lines.append("")
    return "\n".join(detail_lines) + "\n"


def render_digest(
    flow_dirs: list[Path],
    entry_by_flow: dict[str, dict[str, Any]],
    steps_by_flow: dict[str, list[dict[str, Any]]],
    check_by_id: dict[str, dict[str, Any]],
    attached_count: int,
    chosen_count: int,
) -> str:
    """The flattened flow record: a short always-kept index, then the per-step detail."""
    if not flow_dirs and not entry_by_flow:
        return (
            "# UI flow evidence\n\nNo UI flows were declared for this trial.\n"
            "This is expected for a case that commissions none, and for an oracle run.\n"
        )
    if not flow_dirs:
        # Flows WERE declared, but nothing was ever driven -- the executor could not be used at
        # all. Saying "no flows ran" here would read as "this case has no flows", which is a
        # materially different (and prejudicial) claim to put in front of the judge.
        broken = "\n".join(
            "- {}: {} ({})".format(name, entry.get("status") or "unknown", entry.get("reason") or "-")
            for name, entry in sorted(entry_by_flow.items())
        )
        return (
            "# UI flow evidence\n\nUI flows were declared for this trial but none could be driven: "
            "the harness could not drive a browser against the app. This is a failure of the measuring "
            "instrument, not evidence about the delivered app.\n\n{}\n".format(broken)
        )
    index_lines = ["# UI flow evidence", ""]
    for flow_dir in flow_dirs:
        entry = entry_by_flow.get(flow_dir.name, {})
        index_lines.append(
            "- {}: {} ({}), {} step(s) recorded".format(
                flow_dir.name,
                _COMPLETION_BY_STATUS.get(str(entry.get("status") or ""), "no completion recorded"),
                entry.get("reason") or "-",
                len(steps_by_flow.get(flow_dir.name, [])),
            )
        )
    if attached_count == chosen_count:
        attachment_line = "{} screenshot(s) from these flows are attached to this judge request.".format(
            attached_count
        )
    else:
        # Say so rather than let the judge assume it is looking at every frame that was kept: the
        # missing ones are the earliest flows', and their absence is not evidence about the app.
        attachment_line = (
            "{} of {} selected screenshot(s) are attached to this judge request; the earliest flows' "
            "frames were dropped to stay within the attachment ceiling.".format(attached_count, chosen_count)
        )
    index_lines += ["", attachment_line, ""]

    index = "\n".join(index_lines) + "\n"
    # The index is always kept whole: it is what tells the judge how many flows there were and how
    # each came out.
    budget = max(MAX_DIGEST_BYTES - len(index.encode()), 0)
    for earlier_state_chars in _EARLIER_STEP_STATE_LADDER:
        detail = _render_detail(flow_dirs, entry_by_flow, steps_by_flow, check_by_id, earlier_state_chars)
        if len(detail.encode()) <= budget:
            return index + detail
    # Even at the ladder's floor the record does not fit, so whole steps have to go. Keep the TAIL:
    # a flow's last steps carry the state its `expect` is judged against. The marker's own bytes
    # come out of the detail's share, or the result would overshoot the limit this exists to
    # respect.
    marker = "[...earlier steps truncated...]\n"
    detail = _render_detail(flow_dirs, entry_by_flow, steps_by_flow, check_by_id, _EARLIER_STEP_STATE_LADDER[-1])
    tail_budget = max(budget - len(marker.encode()), 0)
    encoded = detail.encode()
    return index + marker + encoded[len(encoded) - tail_budget :].decode(errors="ignore")


def collect_flow_evidence(
    verification_dir: Path, digest_path: Path, screenshots_dir: Path, case_path: Path = CASE_PATH
) -> None:
    """Write the digest and populate the flat screenshot directory. Both always exist afterwards."""
    flow_dirs = _flow_directories(verification_dir)
    # A flow entry points at its evidence directory, so its basename is the join key back to the
    # captured steps and screenshots -- no separate flow-name field is needed on the manifest.
    entry_by_flow = {
        Path(str(entry.get("evidence_path") or "")).name: entry for entry in _flow_entries(verification_dir)
    }
    steps_by_flow = {flow_dir.name: _read_steps(flow_dir) for flow_dir in flow_dirs}
    selected, chosen_count = select_screenshots(
        [_screenshots(flow_dir) for flow_dir in flow_dirs],
        MAX_SCREENSHOTS_PER_FLOW,
        MAX_SCREENSHOTS_TOTAL,
        MAX_SCREENSHOT_BYTES,
    )

    # Recreated rather than topped up, so a regrade never inherits a previous run's selection.
    shutil.rmtree(screenshots_dir, ignore_errors=True)
    if screenshots_dir.exists():
        # It was a file, not a directory, so rmtree left it and mkdir would raise. This pre-step
        # runs under `set -e`, where raising costs the whole grade rather than a screenshot.
        screenshots_dir.unlink()
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    attached_count = 0
    for order, source in enumerate(selected, start=1):
        # Flat and zero-padded: rewardkit lists a directory's files sorted by path string, so
        # unpadded numbering would show the judge step 10 before step 2.
        try:
            shutil.copyfile(source, screenshots_dir / "{:02d}_{}_{}".format(order, source.parent.name, source.name))
        except OSError:
            # A shot that cannot be read is one fewer image for the judge, not a failed grade. It
            # is not counted either, so the digest's number stays the number actually attached.
            continue
        attached_count += 1

    digest_path.parent.mkdir(parents=True, exist_ok=True)
    digest_path.write_text(
        render_digest(
            flow_dirs, entry_by_flow, steps_by_flow, _declared_flows(case_path), attached_count, chosen_count
        )
    )


def main() -> None:
    # test.sh runs this under `set -euo pipefail` and before rewardkit, so an exception here costs
    # the trial its entire reward -- gates, quality and all -- over a flattening step. Whatever went
    # wrong, the judge still gets a digest saying so and an (empty) screenshots directory, because
    # a listed path rewardkit cannot find renders a "[not found]" block into its prompt.
    # Everything this step does is filesystem work and JSON parsing, so those are what it degrades
    # on; a different exception is a bug in this file and should still be loud (the same line
    # outcome/checks.py draws).
    try:
        collect_flow_evidence(VERIFICATION_DIR, DIGEST_PATH, SCREENSHOTS_DIR)
    except (OSError, ValueError, TypeError) as exc:
        print("render_flow_evidence: could not flatten the flow evidence: {}".format(exc), file=sys.stderr)
        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        DIGEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        DIGEST_PATH.write_text(
            "# UI flow evidence\n\nThe flow evidence could not be read ({}). Treat the UI flows as "
            "unmeasured: this is a failure of the harness, not evidence about the delivered app.\n".format(exc)
        )


if __name__ == "__main__":
    main()
