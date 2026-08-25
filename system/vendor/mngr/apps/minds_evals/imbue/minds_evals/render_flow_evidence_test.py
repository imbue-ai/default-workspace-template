"""Unit tests for the grade-time UI-flow flattener. Like the other verifier pre-steps it ships as a
self-contained stdlib script under templates/tests/, so it is loaded by file path rather than
imported as a package module."""

import importlib.util
import json
from pathlib import Path
from typing import Any

_RENDERER_PATH = Path(__file__).parent / "templates" / "tests" / "render_flow_evidence.py"


def _load_renderer() -> Any:
    spec = importlib.util.spec_from_file_location("minds_evals_flow_evidence_renderer", _RENDERER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_RENDERER = _load_renderer()

# A one-pixel PNG, so the sizing rules can be exercised on a real image file.
_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
    "de0000000c4944415408d763f8cfc0000003010100b5e2b0770000000049454e44ae426082"
)


def _write_flow(
    verification_dir: Path, name: str, step_count: int, screenshot_bytes: bytes | None = _PNG_BYTES
) -> Path:
    flow_dir = verification_dir / "flows" / name
    flow_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    for step_index in range(step_count):
        lines.append(
            json.dumps(
                {
                    "step_index": step_index,
                    "action": "click element {}".format(step_index),
                    "reasoning": "step {} of {}".format(step_index, name),
                    "state": "[1]<button> thing {}".format(step_index),
                    "screenshot": "step_{:03d}.png".format(step_index),
                }
            )
        )
        if screenshot_bytes is not None:
            (flow_dir / "step_{:03d}.png".format(step_index)).write_bytes(screenshot_bytes)
    (flow_dir / "log.jsonl").write_text("".join(line + "\n" for line in lines))
    return flow_dir


def _write_manifest(verification_dir: Path, entries: list[dict[str, Any]]) -> None:
    verification_dir.mkdir(parents=True, exist_ok=True)
    (verification_dir / "manifest.json").write_text(json.dumps({"entries": entries}))


def _flow_entry(name: str, status: str, reason: str = "", detail: str = "") -> dict[str, Any]:
    return {
        "entry_id": "ui_flow_0_{}".format(name),
        "check_class": "ui_flows",
        "status": status,
        "reason": reason,
        "detail": detail,
        "evidence_path": "verification/flows/{}".format(name),
    }


def _write_case(tmp_path: Path, checks: list[dict[str, Any]]) -> Path:
    """The expanded case, which is where a flow's declared steps and its `expect` come from."""
    case_path = tmp_path / "case.json"
    case_path.write_text(json.dumps({"expectations": {"ui_flow_checks": checks}}))
    return case_path


def _flow_check(name: str, steps: str = "Open the app.", expect: str = "it survives") -> dict[str, Any]:
    return {"check_id": "ui_flow_0_{}".format(name), "name": name, "steps": steps, "expect": expect}


def _collect(tmp_path: Path, checks: list[dict[str, Any]] | None = None) -> tuple[str, list[str]]:
    digest_path = tmp_path / "judge_flows_digest.txt"
    screenshots_dir = tmp_path / "judge_screenshots"
    _RENDERER.collect_flow_evidence(
        tmp_path / "verification", digest_path, screenshots_dir, _write_case(tmp_path, checks or [])
    )
    return digest_path.read_text(), sorted(path.name for path in screenshots_dir.iterdir())


def test_renderer_flattens_the_flow_logs_and_screenshots(tmp_path: Path) -> None:
    verification_dir = tmp_path / "verification"
    _write_flow(verification_dir, "persistence", step_count=2)
    _write_manifest(verification_dir, [_flow_entry("persistence", "passed")])

    digest, screenshots = _collect(tmp_path, [_flow_check("persistence")])

    assert "persistence: completed (-), 2 step(s) recorded" in digest
    assert "click element 0" in digest and "click element 1" in digest
    assert "[1]<button> thing 1" in digest
    # Flat, and prefixed with a zero-padded order: rewardkit expands a listed directory one level
    # only, and sorts its files by path string.
    assert screenshots == ["01_persistence_step_000.png", "02_persistence_step_001.png"]


def test_renderer_puts_the_open_question_in_front_of_the_judge(tmp_path: Path) -> None:
    # Trial time records completion; the judge rules on the `expect`. It can only do that if the
    # digest carries what was asked for, what was expected, how far the flow got, and what the
    # agent said it saw -- with the agent's reading marked as evidence rather than as an answer.
    verification_dir = tmp_path / "verification"
    flow_dir = _write_flow(verification_dir, "persistence", step_count=1)
    with (flow_dir / "log.jsonl").open("a") as handle:
        handle.write(
            json.dumps(
                {
                    "step_index": 2,
                    "action": _RENDERER.READING_ACTION,
                    "reasoning": "the list shows one task, 'persist me'",
                    "state": "final page",
                    "screenshot": "",
                    "error": "",
                }
            )
            + "\n"
        )
    _write_manifest(verification_dir, [_flow_entry("persistence", "passed")])

    digest, _screenshots = _collect(
        tmp_path,
        [_flow_check("persistence", steps="Add 'persist me'. Reload.", expect="'persist me' is still visible")],
    )

    assert "declared steps: Add 'persist me'. Reload." in digest
    assert "expect (YOU decide whether this holds): 'persist me' is still visible" in digest
    assert "completion: completed (-)" in digest
    assert "reading of the final state, as evidence rather than a verdict: the list shows one task" in digest


def test_renderer_says_a_flow_that_ran_out_of_steps_is_incomplete(tmp_path: Path) -> None:
    # "incomplete" says the flow never got to the end of its declared steps. It must not read as a
    # ruling that the app fell short -- that ruling is the judge's, and it needs the distinction.
    verification_dir = tmp_path / "verification"
    _write_flow(verification_dir, "persistence", step_count=1)
    _write_manifest(verification_dir, [_flow_entry("persistence", "failed", reason="step_budget_exhausted")])

    digest, _screenshots = _collect(tmp_path, [_flow_check("persistence")])

    assert "completion: incomplete (step_budget_exhausted)" in digest


def test_renderer_says_so_when_the_case_declared_no_flows(tmp_path: Path) -> None:
    # A case that commissions none. Both judge inputs must still EXIST: a listed path rewardkit
    # cannot find renders a visible "[not found]" block into the prompt.
    _write_manifest(tmp_path / "verification", [])

    digest, screenshots = _collect(tmp_path)

    assert "No UI flows were declared for this trial." in digest
    assert "[not found]" not in digest
    assert screenshots == []
    assert (tmp_path / "judge_screenshots").is_dir()


def test_renderer_distinguishes_a_broken_browser_from_a_case_with_no_flows(tmp_path: Path) -> None:
    # A fleet that could not be driven leaves manifest entries but no flow directories. Telling the
    # judge "no flows ran" there would read as "this case has no flows" -- a materially different
    # and prejudicial claim, since the judge is asked to grade the delivered app on this evidence.
    _write_manifest(
        tmp_path / "verification",
        [_flow_entry("persistence", "error", reason="chromium_missing")],
    )

    digest, screenshots = _collect(tmp_path)

    assert "none could be driven" in digest
    assert "chromium_missing" in digest
    assert "failure of the measuring instrument" in digest
    assert screenshots == []


def test_renderer_marks_a_step_whose_action_never_ran(tmp_path: Path) -> None:
    verification_dir = tmp_path / "verification"
    flow_dir = verification_dir / "flows" / "persistence"
    flow_dir.mkdir(parents=True)
    (flow_dir / "log.jsonl").write_text(
        json.dumps(
            {
                "step_index": 0,
                "action": "click element 7",
                "reasoning": "the delete button",
                "state": "[1]<a>",
                "error": "no element 7; run `state` first",
            }
        )
        + "\n"
    )
    _write_manifest(verification_dir, [_flow_entry("persistence", "failed", reason="step_budget_exhausted")])

    digest, _screenshots = _collect(tmp_path)

    assert "THIS ACTION DID NOT RUN: no element 7" in digest


def test_renderer_still_produces_both_inputs_when_the_evidence_is_unreadable(tmp_path: Path) -> None:
    # The pre-step runs under `set -e` before rewardkit, so raising costs the trial its entire
    # reward -- gates and quality included -- over a flattening step.
    verification_dir = tmp_path / "verification"
    verification_dir.mkdir(parents=True)
    (verification_dir / "flows").write_text("this is a file where a directory belongs")
    _write_manifest(verification_dir, [])

    digest, screenshots = _collect(tmp_path)

    assert digest
    assert screenshots == []


def test_renderer_states_the_screenshot_count_when_a_flow_captured_none(tmp_path: Path) -> None:
    # An EMPTY listed directory renders nothing at all in rewardkit -- not even a header -- so
    # without this line the judge could not tell "no shots captured" from "no flows declared".
    verification_dir = tmp_path / "verification"
    _write_flow(verification_dir, "persistence", step_count=2, screenshot_bytes=None)
    _write_manifest(verification_dir, [_flow_entry("persistence", "failed", reason="step_budget_exhausted")])

    digest, screenshots = _collect(tmp_path)

    assert screenshots == []
    assert "0 screenshot(s) from these flows are attached" in digest
    assert "persistence: incomplete (step_budget_exhausted)" in digest


def test_renderer_gives_every_flow_its_last_frames(tmp_path: Path) -> None:
    verification_dir = tmp_path / "verification"
    _write_flow(verification_dir, "one", step_count=12)
    _write_manifest(verification_dir, [_flow_entry("one", "passed")])

    _digest, screenshots = _collect(tmp_path)

    assert len(screenshots) == _RENDERER.MAX_SCREENSHOTS_PER_FLOW
    # The END of the flow: the last frame is the state the `expect` is judged against, and the ones
    # before it are what show how the flow got there.
    assert screenshots[-1] == "04_one_step_011.png"
    assert screenshots[0] == "01_one_step_008.png"


def test_renderer_gives_each_flow_its_own_allowance(tmp_path: Path) -> None:
    # A long flow must not spend a later flow's frames: each is guaranteed its own last few.
    verification_dir = tmp_path / "verification"
    _write_flow(verification_dir, "aaa", step_count=12)
    _write_flow(verification_dir, "bbb", step_count=2)
    _write_manifest(verification_dir, [_flow_entry("aaa", "passed"), _flow_entry("bbb", "passed")])

    _digest, screenshots = _collect(tmp_path)

    assert sum(1 for name in screenshots if "_aaa_" in name) == _RENDERER.MAX_SCREENSHOTS_PER_FLOW
    assert sum(1 for name in screenshots if "_bbb_" in name) == 2


def test_renderer_says_so_when_the_attachment_ceiling_drops_frames(tmp_path: Path) -> None:
    # The ceiling is a safety valve on a case with many flows. When it bites, the judge has to know
    # it is looking at a subset -- a missing frame is not evidence about the app.
    verification_dir = tmp_path / "verification"
    flow_count = 1 + _RENDERER.MAX_SCREENSHOTS_TOTAL // _RENDERER.MAX_SCREENSHOTS_PER_FLOW
    entries = []
    for index in range(flow_count):
        name = "flow{:02d}".format(index)
        _write_flow(verification_dir, name, step_count=_RENDERER.MAX_SCREENSHOTS_PER_FLOW)
        entries.append(_flow_entry(name, "passed"))
    _write_manifest(verification_dir, entries)

    digest, screenshots = _collect(tmp_path)

    assert len(screenshots) == _RENDERER.MAX_SCREENSHOTS_TOTAL
    assert (
        "{} of {} selected screenshot(s) are attached".format(
            _RENDERER.MAX_SCREENSHOTS_TOTAL, flow_count * _RENDERER.MAX_SCREENSHOTS_PER_FLOW
        )
        in digest
    )
    # The frames that survive are the LATER flows': the earliest give way first.
    assert not any(name.endswith("flow00_step_000.png") for name in screenshots)


def test_renderer_drops_a_screenshot_rewardkit_would_refuse(tmp_path: Path) -> None:
    # Over 1 MiB rewardkit substitutes a "[skipped: file too large]" marker, which is noise where a
    # smaller frame from the same flow would have been evidence.
    verification_dir = tmp_path / "verification"
    flow_dir = _write_flow(verification_dir, "persistence", step_count=2)
    (flow_dir / "step_001.png").write_bytes(b"\x89PNG" + b"0" * (_RENDERER.MAX_SCREENSHOT_BYTES + 1))
    _write_manifest(verification_dir, [_flow_entry("persistence", "passed")])

    _digest, screenshots = _collect(tmp_path)

    assert screenshots == ["01_persistence_step_000.png"]


def test_renderer_shrinks_earlier_steps_before_the_last_one(tmp_path: Path) -> None:
    # rewardkit drops a judge file over 1 MiB OUTRIGHT, losing the whole record. So an enormous log
    # has to shrink -- and it shrinks from the front: the final state is what the `expect` is judged
    # against, so it keeps its full per-step allowance while earlier steps give up theirs.
    verification_dir = tmp_path / "verification"
    flow_dir = verification_dir / "flows" / "huge"
    flow_dir.mkdir(parents=True)
    final_state = "FINAL" + "z" * 12_000
    (flow_dir / "log.jsonl").write_text(
        "".join(
            json.dumps(
                {
                    "step_index": index,
                    "action": "act",
                    "reasoning": "r",
                    "state": final_state if index == 499 else "x" * 12_000,
                }
            )
            + "\n"
            for index in range(500)
        )
    )
    _write_manifest(verification_dir, [_flow_entry("huge", "passed")])

    digest, _screenshots = _collect(tmp_path)

    assert len(digest.encode()) <= _RENDERER.MAX_DIGEST_BYTES
    assert "huge: completed" in digest
    # The last step's state survives whole; the earlier ones are the ones carrying the marker.
    assert final_state in digest
    assert "[...page state truncated...]" in digest


def test_renderer_drops_earliest_steps_only_when_shrinking_is_not_enough(tmp_path: Path) -> None:
    # Past the smallest shrink threshold there is nothing left to give but whole steps, and it is
    # the earliest that go -- the tail is what the `expect` is judged against.
    verification_dir = tmp_path / "verification"
    flow_dir = verification_dir / "flows" / "huge"
    flow_dir.mkdir(parents=True)
    (flow_dir / "log.jsonl").write_text(
        "".join(
            json.dumps({"step_index": index, "action": "act " + "a" * 4_000, "reasoning": "r", "state": "x"}) + "\n"
            for index in range(500)
        )
    )
    _write_manifest(verification_dir, [_flow_entry("huge", "passed")])

    digest, _screenshots = _collect(tmp_path)

    assert len(digest.encode()) <= _RENDERER.MAX_DIGEST_BYTES
    assert "[...earlier steps truncated...]" in digest


def test_renderer_truncates_one_enormous_page_state_rather_than_the_whole_flow(tmp_path: Path) -> None:
    verification_dir = tmp_path / "verification"
    flow_dir = verification_dir / "flows" / "one"
    flow_dir.mkdir(parents=True)
    (flow_dir / "log.jsonl").write_text(
        json.dumps({"step_index": 0, "action": "act", "reasoning": "r", "state": "y" * 50_000}) + "\n"
    )
    _write_manifest(verification_dir, [_flow_entry("one", "passed")])

    digest, _screenshots = _collect(tmp_path)

    assert "[...page state truncated...]" in digest


def test_renderer_reports_a_flow_whose_entry_never_got_written(tmp_path: Path) -> None:
    # A collector that died mid-phase leaves steps with no manifest entry; the steps are still the
    # most useful thing in the bundle, so they must not be dropped for want of a manifest entry.
    verification_dir = tmp_path / "verification"
    _write_flow(verification_dir, "persistence", step_count=1)
    _write_manifest(verification_dir, [])

    digest, screenshots = _collect(tmp_path)

    assert "persistence: no completion recorded" in digest
    assert screenshots == ["01_persistence_step_000.png"]


def test_renderer_replaces_a_previous_runs_selection(tmp_path: Path) -> None:
    # `harbor trial regrade` re-runs this over an existing directory; a stale shot from an earlier
    # selection would be silently handed to the judge as if it belonged to this one.
    verification_dir = tmp_path / "verification"
    _write_flow(verification_dir, "persistence", step_count=1)
    _write_manifest(verification_dir, [_flow_entry("persistence", "passed")])
    screenshots_dir = tmp_path / "judge_screenshots"
    screenshots_dir.mkdir()
    (screenshots_dir / "99_stale_step_000.png").write_bytes(_PNG_BYTES)

    _digest, screenshots = _collect(tmp_path)

    assert screenshots == ["01_persistence_step_000.png"]
