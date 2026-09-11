import ast
import base64
import subprocess

import pytest

from imbue.minds_admin.slices.home_layout import HomeLayoutAction
from imbue.minds_admin.slices.home_layout import HomeLayoutOutcome
from imbue.minds_admin.slices.home_layout import HomeLayoutStatus
from imbue.minds_admin.slices.home_layout import RESULT_MARKER
from imbue.minds_admin.slices.home_layout import VolumeLayout
from imbue.minds_admin.slices.home_layout import _IN_CONTAINER_QUIESCE_SCRIPT
from imbue.minds_admin.slices.home_layout import _IN_CONTAINER_STALE_CWD_SCRIPT
from imbue.minds_admin.slices.home_layout import _IN_CONTAINER_SWITCH_SCRIPT
from imbue.minds_admin.slices.home_layout import _IN_CONTAINER_WAIT_SERVICES_SCRIPT
from imbue.minds_admin.slices.home_layout import build_home_layout_report
from imbue.minds_admin.slices.home_layout import build_vm_script
from imbue.minds_admin.slices.home_layout import parse_vm_script_output


def _detail(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


@pytest.mark.parametrize("action", list(HomeLayoutAction))
def test_vm_script_is_valid_bash_for_every_action(action: HomeLayoutAction) -> None:
    """The script is assembled from f-strings with embedded quoting; a syntax slip would only surface on a live VM."""
    completed = subprocess.run(
        ["bash", "-n"], input=build_vm_script(action), capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    "script",
    [_IN_CONTAINER_QUIESCE_SCRIPT, _IN_CONTAINER_WAIT_SERVICES_SCRIPT, _IN_CONTAINER_STALE_CWD_SCRIPT],
    ids=["quiesce", "wait-services", "stale-cwd"],
)
def test_embedded_shell_scripts_are_valid_bash(script: str) -> None:
    """These travel base64-encoded inside the VM script, so the VM script's own syntax check never sees them."""
    completed = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr


def test_embedded_switch_script_is_valid_python() -> None:
    ast.parse(_IN_CONTAINER_SWITCH_SCRIPT)


def test_parse_reads_the_probe_then_the_verdict() -> None:
    stdout = "\n".join(
        [
            "quiescing the workspace",
            f"{RESULT_MARKER} probe=1 layout=legacy container=minds-spare-051 home_bytes=3221225472 free_bytes=29000000000",
            "copying /home/user out of the container onto the volume",
            f"{RESULT_MARKER} status=migrated detail_b64={_detail('home tree now on the volume; rollback at /mnt/x/rollback/pre-home-layout-1')}",
        ]
    )
    status, detail, probe = parse_vm_script_output(stdout)
    assert status == HomeLayoutStatus.MIGRATED
    assert detail.startswith("home tree now on the volume")
    assert probe is not None
    assert probe.layout == VolumeLayout.LEGACY
    assert probe.container == "minds-spare-051"
    assert probe.home_bytes == 3221225472
    assert probe.free_bytes == 29000000000


def test_parse_treats_a_cut_short_script_as_failed_but_keeps_the_probe() -> None:
    stdout = f"{RESULT_MARKER} probe=1 layout=home container=c home_bytes=1 free_bytes=2\nconnection closed"
    status, detail, probe = parse_vm_script_output(stdout)
    assert status == HomeLayoutStatus.FAILED
    assert "no verdict" in detail
    assert probe is not None
    assert probe.layout == VolumeLayout.HOME


def test_parse_rejects_an_unknown_verdict_and_layout() -> None:
    stdout = (
        f"{RESULT_MARKER} probe=1 layout=sideways container=c home_bytes=x free_bytes=2\n"
        f"{RESULT_MARKER} status=exploded detail_b64={_detail('boom')}"
    )
    status, detail, probe = parse_vm_script_output(stdout)
    assert status == HomeLayoutStatus.FAILED
    assert "unrecognized verdict" in detail
    assert probe is not None
    assert probe.layout == VolumeLayout.UNKNOWN
    assert probe.home_bytes == 0


def test_report_counts_each_status_once_and_carries_the_unreachable_ids() -> None:
    def outcome(name: str, status: HomeLayoutStatus) -> HomeLayoutOutcome:
        return HomeLayoutOutcome(
            host_id=f"host-{name}", host_name=name, vm_name=f"vm-{name}", server_id="s", status=status
        )

    report = build_home_layout_report(
        [
            outcome("a", HomeLayoutStatus.MIGRATED),
            outcome("b", HomeLayoutStatus.LEGACY_LAYOUT),
            outcome("c", HomeLayoutStatus.BLOCKED),
            outcome("d", HomeLayoutStatus.FAILED),
            outcome("e", HomeLayoutStatus.MIGRATED),
        ],
        ["host-z"],
    )
    assert (report.migrated, report.legacy_layout, report.blocked, report.failed, report.home_layout) == (
        2,
        1,
        1,
        1,
        0,
    )
    assert report.unreachable == ("host-z",)
    assert len(report.outcomes) == 5
