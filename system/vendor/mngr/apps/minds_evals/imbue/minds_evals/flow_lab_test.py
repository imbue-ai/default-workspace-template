"""What the flow lab prints for each kind of log record.

`describe_record` reads a `log.jsonl` line by key, and the keys come from `ui_flows`'s record
writers, so the two are built here from the writers themselves rather than from hand-written JSON:
a renamed key has to show up as a rendering that lost its content, not as a test that agrees with
the renderer about a shape neither of them writes.
"""

import os
import threading
from pathlib import Path

import pytest

from imbue.minds_evals import flow_lab
from imbue.minds_evals import ui_flows
from imbue.minds_evals.resources.flow_step_protocol import StepReaction
from imbue.mngr.utils.polling import wait_for


def test_the_opening_record_renders_as_the_url_it_opened() -> None:
    record = ui_flows.flow_init_record(
        "Add a task.",
        "the task is listed",
        "http://127.0.0.1:8000/?latency=300",
        "page ...",
        "step_000.png",
        2048,
        True,
        "now",
    )

    assert flow_lab.describe_record(record) == "opened http://127.0.0.1:8000/?latency=300"


def test_an_action_record_renders_as_its_step_in_the_agents_history() -> None:
    record = ui_flows.flow_step_record(
        3,
        'click the button named "Add"',
        "",
        "the control is on the page",
        "the task is listed",
        'new: - listitem: "walk dog"',
        StepReaction.SETTLED,
        "page ...",
        "step_003.png",
        2048,
        True,
        "",
        "now",
    )

    assert flow_lab.describe_record(record) == "3. " + ui_flows.describe_step(
        'click the button named "Add"', "the task is listed", "", 'new: - listitem: "walk dog"'
    )


def test_a_step_that_did_not_run_renders_with_its_error() -> None:
    record = ui_flows.flow_step_record(
        4,
        'click the button named "Delete"',
        "",
        "",
        "the task is gone",
        "",
        StepReaction.UNOBSERVED,
        "page ...",
        "",
        0,
        False,
        "no such element",
        "now",
    )

    assert "did not run: no such element" in flow_lab.describe_record(record)


def test_the_closing_record_renders_as_the_agents_reading() -> None:
    record = ui_flows.flow_final_record(5, "the list holds one task", "page ...", "now")

    assert flow_lab.describe_record(record) == "reading: the list holds one task"


def test_a_closing_record_with_no_reading_says_so_rather_than_printing_a_blank() -> None:
    record = ui_flows.flow_final_record(5, "", "page ...", "now")

    assert flow_lab.describe_record(record) == "reading: (none recorded)"


def test_fresh_profile_dir_is_removed_on_exit() -> None:
    with flow_lab.fresh_profile_dir() as profile_dir:
        (profile_dir / "Default").mkdir()
        (profile_dir / "Default" / "Preferences").write_text("{}")
        assert profile_dir.is_dir()
    assert not profile_dir.exists()


@pytest.mark.skipif(
    os.geteuid() == 0, reason="root removes a read-only directory's children, so no removal can fall short"
)
def test_a_removal_that_falls_short_reports_the_entry_that_stopped_it_not_the_parent_left_behind(
    tmp_path: Path,
) -> None:
    profile_dir = tmp_path / "profile"
    (profile_dir / "Default").mkdir(parents=True)
    (profile_dir / "Default" / "Preferences").write_text("{}")
    profile_dir.chmod(0o500)
    removal = flow_lab._ProfileRemoval(directory=profile_dir)
    try:
        is_removed = removal.try_remove()
    finally:
        profile_dir.chmod(0o700)

    assert not is_removed
    assert removal.cause.startswith(str(profile_dir / "Default") + ": ")
    assert "Permission denied" in removal.cause
    assert "Directory not empty" not in removal.cause


@pytest.mark.skipif(
    os.geteuid() == 0, reason="root removes a read-only directory's children, so no removal can fall short"
)
def test_fresh_profile_dir_retries_when_a_removal_leaves_the_tree_behind() -> None:
    """Stands in for Chromium's helpers, which keep the first removal from taking the tree down.

    A read-only profile dir gives the first removal the same shape as a refilled Default/: it empties
    Default/ but cannot remove it, so the tree is still there afterwards and cleanup has to try again.
    """
    is_tree_left_by_first_removal = threading.Event()

    def let_go_once_the_first_removal_has_run(profile_dir: Path, preferences: Path) -> None:
        # Preferences vanishing is the first removal's trace: it is unlinked before the read-only parent stops the rmdir.
        wait_for(lambda: not preferences.exists(), timeout=5.0)
        if profile_dir.exists():
            is_tree_left_by_first_removal.set()
            profile_dir.chmod(0o700)

    with flow_lab.fresh_profile_dir() as profile_dir:
        preferences = profile_dir / "Default" / "Preferences"
        preferences.parent.mkdir()
        preferences.write_text("{}")
        profile_dir.chmod(0o500)
        releaser = threading.Thread(
            target=let_go_once_the_first_removal_has_run, args=(profile_dir, preferences), name="profile-releaser"
        )
        releaser.start()
    releaser.join(timeout=5.0)

    assert is_tree_left_by_first_removal.is_set()
    assert not profile_dir.exists()
