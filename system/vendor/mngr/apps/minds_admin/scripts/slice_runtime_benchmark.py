"""Run the runc-vs-runsc slice benchmark and write its markdown report.

Operator tooling for the phase-1 checkpoint of the slice-fleet cutover plan
(``blueprint/slice-fleet-cutover/``); deleted with the cutover tooling in
phase 6. Point it at leased gen-2 slices on one box, one per runtime label:

    uv run python apps/minds_admin/scripts/slice_runtime_benchmark.py \\
        --box-address 51.81.208.81 --pool-key /path/to/pool-key \\
        --slice runc=22002 --slice runsc=22004 --dwt-ref new-fleet-runsc-prototype \\
        --output blueprint/slice-fleet-cutover/benchmark-2026-08-27.md

The pool key must be authorized on each VM's root (it is, since the bake).
Pass ``--container-env ANTHROPIC_API_KEY=...`` for the ``claude -p`` round
trip; without it that workload records itself as skipped.
"""

from datetime import datetime
from datetime import timezone
from pathlib import Path

import click
from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import setup_logging
from imbue.minds_admin.slices.runtime_benchmark import BenchmarkWorkload
from imbue.minds_admin.slices.runtime_benchmark import build_checkpoint_workloads
from imbue.minds_admin.slices.runtime_benchmark import render_report_markdown
from imbue.minds_admin.slices.runtime_benchmark import run_benchmark


class BenchmarkCliArguments(FrozenModel):
    """Parsed command line arguments for the slice runtime benchmark."""

    box_address: str = Field(description="The box's public address (the slices' forwarded ports live on it)")
    pool_key_path: Path = Field(description="Private key authorized on every slice VM's root")
    vm_ssh_port_by_label: dict[str, int] = Field(description="Slice label -> the box port forwarded to its VM sshd")
    container_env: dict[str, str] = Field(description="Environment exported inside every container workload")
    dwt_ref: str = Field(description="The default-workspace-template ref the slices were baked from")
    output_path: Path = Field(description="Where the markdown report is written")
    only_workloads: tuple[str, ...] = Field(description="Run only these workload names (empty = all)")


def _parse_pairs(entries: tuple[str, ...], flag: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for entry in entries:
        key, separator, value = entry.partition("=")
        if not separator or not key or not value:
            raise click.UsageError(f"{flag} expects KEY=VALUE, got {entry!r}")
        parsed[key] = value
    return parsed


def _parse_slice_ports(entries: tuple[str, ...]) -> dict[str, int]:
    """``--slice LABEL=VM_SSH_PORT`` entries as label -> port; a usage error for a non-numeric or non-positive port."""
    vm_ssh_port_by_label: dict[str, int] = {}
    for label, port_text in _parse_pairs(entries, "--slice").items():
        try:
            port = int(port_text)
        except ValueError:
            raise click.UsageError(
                f"--slice expects LABEL=VM_SSH_PORT with a numeric port, got {port_text!r}"
            ) from None
        if port <= 0:
            raise click.UsageError(f"--slice port must be positive, got {port}")
        vm_ssh_port_by_label[label] = port
    return vm_ssh_port_by_label


def _select_workloads(only_workloads: tuple[str, ...]) -> tuple[BenchmarkWorkload, ...]:
    """The checkpoint workloads to run, in their fixed order; a usage error names any unknown ``--only`` entry."""
    workloads = build_checkpoint_workloads()
    if not only_workloads:
        return workloads
    known_names = [workload.name for workload in workloads]
    unknown_names = sorted(set(only_workloads) - set(known_names))
    if unknown_names:
        raise click.UsageError(
            f"--only names unknown workload(s) {unknown_names}; the workloads are: {', '.join(known_names)}"
        )
    return tuple(workload for workload in workloads if workload.name in only_workloads)


@click.command()
@click.option("--box-address", required=True, help="The box's public address.")
@click.option(
    "--pool-key",
    "pool_key_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Private key authorized on every slice VM's root (the tier's pool key).",
)
@click.option(
    "--slice",
    "slices",
    multiple=True,
    required=True,
    help="LABEL=VM_SSH_PORT for one leased slice (repeat; labels runc/runsc get the ratio column).",
)
@click.option(
    "--container-env",
    "container_env",
    multiple=True,
    help="KEY=VALUE exported inside every container workload (e.g. ANTHROPIC_API_KEY for claude -p).",
)
@click.option("--dwt-ref", required=True, help="The default-workspace-template ref the slices were baked from.")
@click.option(
    "--output",
    "output_path",
    required=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Where the markdown report is written.",
)
@click.option("--only", "only_workloads", multiple=True, help="Run only these workload names (repeat).")
def main(
    box_address: str,
    pool_key_path: Path,
    slices: tuple[str, ...],
    container_env: tuple[str, ...],
    dwt_ref: str,
    output_path: Path,
    only_workloads: tuple[str, ...],
) -> None:
    """Run the fixed workload set on each slice and write the comparison report."""
    setup_logging(level="INFO")
    arguments = BenchmarkCliArguments(
        box_address=box_address,
        pool_key_path=pool_key_path,
        vm_ssh_port_by_label=_parse_slice_ports(slices),
        container_env=_parse_pairs(container_env, "--container-env"),
        dwt_ref=dwt_ref,
        output_path=output_path,
        only_workloads=only_workloads,
    )
    workloads = _select_workloads(arguments.only_workloads)
    report = run_benchmark(
        box_address=arguments.box_address,
        private_key_path=arguments.pool_key_path,
        vm_ssh_port_by_label=arguments.vm_ssh_port_by_label,
        workloads=workloads,
        container_env=arguments.container_env,
        dwt_ref=arguments.dwt_ref,
        generated_at=datetime.now(timezone.utc),
    )
    arguments.output_path.parent.mkdir(parents=True, exist_ok=True)
    arguments.output_path.write_text(render_report_markdown(report, workloads))
    logger.info("Wrote the benchmark report to {}", arguments.output_path)


if __name__ == "__main__":
    main()
