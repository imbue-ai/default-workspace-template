import os
import subprocess
from datetime import datetime
from datetime import timezone

from inline_snapshot import snapshot

from imbue.minds_admin.slices.runtime_benchmark import BenchmarkReport
from imbue.minds_admin.slices.runtime_benchmark import BenchmarkWorkload
from imbue.minds_admin.slices.runtime_benchmark import SliceFacts
from imbue.minds_admin.slices.runtime_benchmark import WorkloadLocation
from imbue.minds_admin.slices.runtime_benchmark import _CONTAINER_PRELUDE
from imbue.minds_admin.slices.runtime_benchmark import _timed
from imbue.minds_admin.slices.runtime_benchmark import build_checkpoint_workloads
from imbue.minds_admin.slices.runtime_benchmark import parse_workload_output
from imbue.minds_admin.slices.runtime_benchmark import render_report_markdown
from imbue.minds_admin.slices.runtime_benchmark import summarize_runs


def test_checkpoint_workloads_have_unique_names_and_cover_the_plans_list() -> None:
    workloads = build_checkpoint_workloads()
    names = [workload.name for workload in workloads]
    assert len(names) == len(set(names))
    # The plan's fixed set: cold uv sync, npm ci, a git clone, Fortress launch,
    # Claude Code startup, terminal/SSH latency, earlyoom under pressure, plus
    # the prototype micro-benchmarks for continuity.
    for required in (
        "uv_sync_cold",
        "npm_ci_system_interface",
        "git_clone_flask",
        "fortress_first_page",
        "claude_version",
        "claude_prompt_round_trip",
        "container_ssh_connect_latency",
        "earlyoom_pressure",
        "find_workspace",
        "tar_workspace",
        "git_status",
        "syscall_loop_dd",
    ):
        assert required in names
    # Only the connect-latency probe runs on the VM; everything else is a
    # container workload, and every script passes a syntax check.
    for workload in workloads:
        expected_location = (
            WorkloadLocation.VM if workload.name == "container_ssh_connect_latency" else WorkloadLocation.CONTAINER
        )
        assert workload.location == expected_location
        body = workload.script if workload.location == WorkloadLocation.VM else _CONTAINER_PRELUDE + workload.script
        result = subprocess.run(["bash", "-n"], input=body, capture_output=True, text=True)
        assert result.returncode == 0, f"{workload.name}: {result.stderr}"


def test_prelude_timing_helpers_emit_the_seconds_and_exit_lines(local_container_prelude: str) -> None:
    script = local_container_prelude + "bench_start\ntrue\nbench_stop $?\nnote hello world\n"
    result = subprocess.run(
        ["bash", "-s"],
        input=script,
        capture_output=True,
        text=True,
        env=dict(os.environ),
    )
    assert result.returncode == 0, result.stderr
    seconds, workload_exit, notes = parse_workload_output(result.stdout, result.returncode)
    assert seconds is not None and 0 <= seconds < 5
    assert workload_exit == 0
    assert notes == ("hello world",)


def test_timed_wrapper_covers_a_whole_pipeline(local_container_prelude: str) -> None:
    # The /dev/null stdin must apply to the whole timed command: bound to the
    # last stage of a pipeline it would starve the consumer (``wc -l``) and end
    # the pipeline before the producer ran, recording a bogus fast success.
    script = local_container_prelude + _timed("printf 'x\\n' | grep -q x")
    result = subprocess.run(["bash", "-s"], input=script, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    _seconds, workload_exit, _notes = parse_workload_output(result.stdout, result.returncode)
    assert workload_exit == 0


def test_parse_workload_output_reads_only_the_marker_lines() -> None:
    stdout = "noise\nMNGR_BENCH_NOTE=uv=uv 0.9\nMNGR_BENCH_SECONDS=12.5000\nMNGR_BENCH_EXIT=1\ntrailing\n"
    assert parse_workload_output(stdout, 0) == (12.5, 1, ("uv=uv 0.9",))
    # A transport failure with no marker lines is recorded as such.
    assert parse_workload_output("ssh: connect refused\n", 255) == (
        None,
        None,
        ("transport exit 255",),
    )


def _workload(name: str, repetitions: int) -> BenchmarkWorkload:
    return BenchmarkWorkload(
        name=name,
        description=name,
        location=WorkloadLocation.CONTAINER,
        script="true",
        timeout_seconds=10,
        repetitions=repetitions,
    )


def test_summarize_runs_takes_the_median_of_successful_runs_and_flags_failures() -> None:
    workload = _workload("find", 3)
    clean = summarize_runs(workload, "runsc", [(0.9, 0, ()), (0.5, 0, ("n1",)), (0.7, 0, ())])
    assert clean.seconds == 0.7
    assert clean.notes == ("n1",)
    assert clean.error is None
    partial = summarize_runs(workload, "runsc", [(0.9, 0, ()), (1.5, 2, ()), (0.7, 0, ())])
    assert partial.seconds == 0.8
    assert partial.error == "1 of 3 runs failed (exit [2])"
    all_failed = summarize_runs(workload, "runsc", [(None, 95, ("fortress not installed",))])
    assert all_failed.seconds is None
    assert all_failed.error == "every run failed"
    transport = summarize_runs(workload, "runc", [(None, None, ("transport exit 255",))])
    assert transport.error == "no measurement (transport or preflight failure)"


def test_report_markdown_carries_the_ratio_column_and_the_notes() -> None:
    workloads = (_workload("find", 3), _workload("uv_sync_cold", 1))
    report = BenchmarkReport(
        generated_at=datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc),
        box_address="51.81.208.81",
        dwt_ref="new-fleet-runsc-prototype",
        slices=(
            SliceFacts(
                label="runc",
                vm_ssh_port=22002,
                container_runtime="runc",
                container_kernel="6.12.96+deb13-cloud-amd64",
                container_memory_bytes=7516192768,
                container_vcpus=2,
                tmpfs_mounts=("/run", "/tmp"),
                vm_memory_used_mib_before=900,
                vm_memory_used_mib_after=1200,
                container_memory_usage_after="754MiB / 7GiB",
            ),
            SliceFacts(
                label="runsc",
                vm_ssh_port=22004,
                container_runtime="runsc",
                container_kernel="4.19.0-gvisor",
                container_memory_bytes=7516192768,
                container_vcpus=2,
                tmpfs_mounts=("/run", "/tmp"),
                vm_memory_used_mib_before=1000,
                vm_memory_used_mib_after=1800,
                container_memory_usage_after="1GiB / 7GiB",
            ),
        ),
        measurements=(
            summarize_runs(workloads[0], "runc", [(0.12, 0, ())]),
            summarize_runs(workloads[0], "runsc", [(0.6, 0, ())]),
            summarize_runs(workloads[1], "runc", [(120.0, 0, ("uv=uv 0.9",))]),
            summarize_runs(workloads[1], "runsc", [(None, 1, ())]),
        ),
    )
    assert render_report_markdown(report, workloads) == snapshot("""\
# runc vs runsc slice benchmark (2026-08-27)

Box `51.81.208.81`, default-workspace-template ref `new-fleet-runsc-prototype`, generated 2026-08-27 12:00 UTC by `apps/minds_admin/scripts/slice_runtime_benchmark.py`.

## Slices

| label | VM port | container runtime | container kernel | vCPUs | memory cap | tmpfs |
|---|---|---|---|---|---|---|
| runc | 22002 | runc | 6.12.96+deb13-cloud-amd64 | 2 | 7.00 GiB | /run, /tmp |
| runsc | 22004 | runsc | 4.19.0-gvisor | 2 | 7.00 GiB | /run, /tmp |

## Timings (median of the repetitions)

| workload | runc | runsc | runsc / runc |
|---|---|---|---|
| `find` | 0.12 s | 0.60 s | 5.00x |
| `uv_sync_cold` | 120.00 s | - (!) | - |

(!) marks a workload where some run failed; see the notes.

## Workloads

- `find`: find (container, x3)
- `uv_sync_cold`: uv_sync_cold (container, x1)

## Memory footprint

| label | VM used before | VM used after | container (docker stats) after |
|---|---|---|---|
| runc | 900 MiB | 1200 MiB | 754MiB / 7GiB |
| runsc | 1000 MiB | 1800 MiB | 1GiB / 7GiB |

## Notes

- `uv_sync_cold` on runc:
  - uv=uv 0.9
- `uv_sync_cold` on runsc:
  - error: every run failed
""")
