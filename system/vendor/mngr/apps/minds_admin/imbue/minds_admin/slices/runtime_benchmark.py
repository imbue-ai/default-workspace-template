"""The runc-vs-runsc slice benchmark for the slice-fleet cutover checkpoint.

Runs one fixed workload set against leased gen-2 slices (over each slice's
VM-root SSH, then ``docker exec`` into the workspace container) and renders
the comparison as a markdown report under ``blueprint/slice-fleet-cutover/``.
Operator tooling for the phase-1 checkpoint of the cutover plan; deleted with
the cutover tooling in phase 6.

Every workload is a bash script that prints ``MNGR_BENCH_SECONDS=<float>``
(timed inside the guest with bash's ``EPOCHREALTIME``, so SSH round-trips
never pollute a measurement), ``MNGR_BENCH_EXIT=<rc>``, and any number of
``MNGR_BENCH_NOTE=<text>`` lines; the runner parses those and nothing else.
"""

import base64
import shlex
import statistics
import tempfile
from abc import ABC
from abc import abstractmethod
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from enum import auto
from pathlib import Path
from typing import Final
from typing import assert_never

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds.errors import MindError
from imbue.mngr_vps.container_setup import LABEL_HOST_ID


class SliceBenchmarkError(MindError):
    """A slice could not be reached or readied for the benchmark."""


# The two runtimes the checkpoint compares; the report's ratio column is
# runsc over runc when both labels are present.
RUNC_LABEL: Final[str] = "runc"
RUNSC_LABEL: Final[str] = "runsc"

_SECONDS_PREFIX: Final[str] = "MNGR_BENCH_SECONDS="
_EXIT_PREFIX: Final[str] = "MNGR_BENCH_EXIT="
_NOTE_PREFIX: Final[str] = "MNGR_BENCH_NOTE="

# How long a freshly-leased slice may take to bring its services up (the
# autostart relaunch of the services agent, then env-converge installing the
# Fortress browser -- a chromium download) before the workloads run.
_SERVICES_READY_TIMEOUT_SECONDS: Final[float] = 900.0
_FORTRESS_BINARY: Final[str] = "/opt/fortress/tilion-fortress/tilion"
_SERVICES_AGENT_START_SCRIPT: Final[str] = "/home/user/workspace/system/scripts/minds_start_services_agent.sh"
_SHED_LEDGER_PATH: Final[str] = "/home/user/workspace/data/.state/oom_priority/events/shed.jsonl"

# Puts the workspace container's id in `cid`. stdin is redirected because every
# script here arrives on the target shell's stdin (`bash -s`).
_CONTAINER_ID_LOOKUP: Final[str] = f"cid=$(docker ps -q --filter label={LABEL_HOST_ID} </dev/null | head -1)"

# Prepended to every container workload: the workspace layout's PATH (uv,
# node, claude live under root's home, which the image points at /home/user)
# and the two timing helpers every workload brackets its command with.
_CONTAINER_PRELUDE: Final[str] = """\
set -u
export PATH="/home/user/.local/bin:/home/user/.npm-global/bin:/usr/local/bin:$PATH"
export HOME=/home/user
cd /home/user/workspace
bench_start() { MNGR_BENCH_START="$EPOCHREALTIME"; }
bench_stop() {
    MNGR_BENCH_RC="$1"
    awk -v a="$MNGR_BENCH_START" -v b="$EPOCHREALTIME" 'BEGIN{printf "MNGR_BENCH_SECONDS=%.4f\\n", b-a}'
    echo "MNGR_BENCH_EXIT=$MNGR_BENCH_RC"
}
note() { echo "MNGR_BENCH_NOTE=$*"; }
"""


class WorkloadLocation(UpperCaseStrEnum):
    """Where a workload's bash runs: inside the workspace container, or on the VM root."""

    CONTAINER = auto()
    VM = auto()


class BenchmarkWorkload(FrozenModel):
    """One timed workload of the checkpoint's fixed set."""

    name: str = Field(description="Stable identifier (the report's row key)")
    description: str = Field(description="One line for the report")
    location: WorkloadLocation = Field(description="Container or VM root")
    script: str = Field(description="Bash body that prints the MNGR_BENCH_* lines (the prelude is prepended)")
    timeout_seconds: float = Field(description="Hard timeout for one run")
    repetitions: int = Field(description="Runs per slice; the report shows the median")


class WorkloadMeasurement(FrozenModel):
    """The parsed result of one workload on one slice (the median of its repetitions)."""

    workload_name: str = Field(description="The workload's identifier")
    slice_label: str = Field(description="The slice's label (runc / runsc)")
    seconds: float | None = Field(description="Median wall time; None when every run failed")
    notes: tuple[str, ...] = Field(description="Every MNGR_BENCH_NOTE line, in order")
    error: str | None = Field(description="Why the measurement is missing or suspect, if anything")


class SliceFacts(FrozenModel):
    """What the benchmark saw when it first reached a slice."""

    label: str = Field(description="The slice's label (runc / runsc)")
    vm_ssh_port: int = Field(description="The box port forwarded to the VM root sshd")
    container_runtime: str = Field(description="docker inspect .HostConfig.Runtime")
    container_kernel: str = Field(description="uname -r inside the container (4.19.0-gvisor under runsc)")
    container_memory_bytes: int = Field(description="docker inspect .HostConfig.Memory")
    container_vcpus: int = Field(description="nproc inside the container")
    tmpfs_mounts: tuple[str, ...] = Field(description="Which of /run and /tmp are tmpfs inside the container")
    vm_memory_used_mib_before: int = Field(description="The VM's `free -m` used column before the workloads")
    vm_memory_used_mib_after: int | None = Field(
        description="The VM's `free -m` used column after the workloads; None until the workloads have run"
    )
    container_memory_usage_after: str | None = Field(
        description="docker stats MemUsage after the workloads; None until the workloads have run"
    )


class BenchmarkReport(FrozenModel):
    """Everything the markdown report is rendered from."""

    generated_at: datetime = Field(description="When the run finished (UTC)")
    box_address: str = Field(description="The box the slices live on")
    dwt_ref: str = Field(description="The default-workspace-template ref both slices were baked from")
    slices: tuple[SliceFacts, ...] = Field(description="Per-slice facts, in the order given")
    measurements: tuple[WorkloadMeasurement, ...] = Field(description="One per (workload, slice)")


class SliceShellInterface(MutableModel, ABC):
    """Runs bash on one leased slice: on its VM root, or inside its workspace container."""

    label: str = Field(frozen=True, description="The slice's label (runc / runsc)")

    @abstractmethod
    def run_on_vm(self, script: str, *, timeout_seconds: float) -> tuple[int, str, str]:
        """Run ``script`` as root on the VM; return (exit code, stdout, stderr)."""

    @abstractmethod
    def run_in_container(self, script: str, *, timeout_seconds: float) -> tuple[int, str, str]:
        """Run ``script`` in a login shell inside the mngr-labeled container; return (exit code, stdout, stderr)."""


class SshSliceShell(SliceShellInterface):
    """The real shell: ssh to the slice's VM root with the pool key, docker exec for the container."""

    box_address: str = Field(frozen=True, description="The box's public address")
    vm_ssh_port: int = Field(frozen=True, description="The box port forwarded to the VM root sshd")
    private_key_path: Path = Field(frozen=True, description="A key authorized on the VM root (the pool key)")
    known_hosts_path: Path = Field(
        frozen=True, description="A scratch known_hosts file (first contact with a slice this run leased is TOFU)"
    )

    def _ssh(self, remote_command: str, script: str, *, timeout_seconds: float) -> tuple[int, str, str]:
        # The script rides base64-encoded inside the remote command and is
        # decoded into the target shell's stdin there, so nothing is quoted
        # through two shells and no local stdin plumbing is needed.
        encoded = base64.b64encode(script.encode()).decode()
        remote = f"echo {shlex.quote(encoded)} | base64 -d | {remote_command}"
        cg = ConcurrencyGroup(name=f"slice-benchmark-{self.label}")
        with cg:
            result = cg.run_process_to_completion(
                command=[
                    "ssh",
                    "-i",
                    str(self.private_key_path),
                    "-p",
                    str(self.vm_ssh_port),
                    "-o",
                    "StrictHostKeyChecking=accept-new",
                    "-o",
                    f"UserKnownHostsFile={self.known_hosts_path}",
                    "-o",
                    "ConnectTimeout=30",
                    "-o",
                    "ServerAliveInterval=30",
                    f"root@{self.box_address}",
                    remote,
                ],
                timeout=timeout_seconds,
                is_checked_after=False,
            )
        return result.returncode if result.returncode is not None else 1, result.stdout, result.stderr

    def run_on_vm(self, script: str, *, timeout_seconds: float) -> tuple[int, str, str]:
        return self._ssh("bash -s", script, timeout_seconds=timeout_seconds)

    def run_in_container(self, script: str, *, timeout_seconds: float) -> tuple[int, str, str]:
        # A login shell so the image's profile applies. The lookup and the exec
        # form one group so the piped script reaches docker exec's stdin and the
        # container id assignment is not lost to a pipeline subshell.
        remote = (
            f"{{ {_CONTAINER_ID_LOOKUP}; "
            '[ -n "$cid" ] || { echo "no mngr-labeled container running" >&2; exit 97; }; '
            'docker exec -i --workdir / "$cid" bash -l -s; }'
        )
        return self._ssh(remote, script, timeout_seconds=timeout_seconds)


@pure
def _timed(command: str) -> str:
    """Wrap one command in the timing brackets (its output is discarded; the exit code is recorded).

    stdin comes from /dev/null: the script itself arrives on the shell's stdin,
    and a command that reads stdin (``claude -p`` does) would otherwise swallow
    the rest of the script, timing brackets included. The command runs inside a
    brace group so the redirections apply to the whole of it -- attached to a
    bare pipeline they would bind to its last stage only, and a consumer such as
    ``wc -l`` reading /dev/null would end the pipeline before the producer ran.
    """
    return f"bench_start\n{{\n{command}\n}} </dev/null >/dev/null 2>&1\nbench_stop $?\n"


@pure
def _timed_until_killed(command: str) -> str:
    """Time a command whose expected end is being killed: the kill is the result, not a failure."""
    return (
        f"bench_start\n{{\n{command}\n}} </dev/null >/dev/null 2>&1\nMNGR_BENCH_KILLED_RC=$?\nbench_stop 0\n"
        'note "command_exit=$MNGR_BENCH_KILLED_RC"\n'
    )


@pure
def _repeat_timed(command: str, count: int) -> str:
    """Time ``count`` back-to-back runs of a fast command as one measurement."""
    return _timed(f"for _ in $(seq {count}); do {command} || exit $?; done")


@pure
def build_checkpoint_workloads() -> tuple[BenchmarkWorkload, ...]:
    """The fixed workload set of the phase-1 checkpoint (plan: "a fixed benchmark script")."""
    container = WorkloadLocation.CONTAINER
    return (
        BenchmarkWorkload(
            name="python_startup_x20",
            description="`python3 -c pass` x20",
            location=container,
            script=_repeat_timed("python3 -c pass", 20),
            timeout_seconds=120,
            repetitions=3,
        ),
        BenchmarkWorkload(
            name="node_startup_x20",
            description="`node -e 0` x20",
            location=container,
            script=_repeat_timed("node -e 0", 20),
            timeout_seconds=120,
            repetitions=3,
        ),
        BenchmarkWorkload(
            name="find_workspace",
            description="`find /home/user/workspace -type f | wc -l`",
            location=container,
            script=_timed("find /home/user/workspace -type f | wc -l"),
            timeout_seconds=300,
            repetitions=3,
        ),
        BenchmarkWorkload(
            name="tar_workspace",
            description="`tar -cf /dev/null workspace`",
            location=container,
            script=_timed("tar -cf /dev/null -C /home/user workspace"),
            timeout_seconds=600,
            repetitions=3,
        ),
        BenchmarkWorkload(
            name="git_status",
            description="`git status --porcelain` in the workspace",
            location=container,
            script=_timed("git -C /home/user/workspace status --porcelain"),
            timeout_seconds=300,
            repetitions=3,
        ),
        BenchmarkWorkload(
            name="compileall_mngr",
            description="`python3 -m compileall -f` over the vendored libs/mngr",
            location=container,
            script=_timed("python3 -m compileall -q -f /home/user/workspace/system/vendor/mngr/libs/mngr"),
            timeout_seconds=600,
            repetitions=3,
        ),
        BenchmarkWorkload(
            name="syscall_loop_dd",
            description="`dd if=/dev/zero of=/dev/null bs=4k count=200000` (syscall loop)",
            location=container,
            script=_timed("dd if=/dev/zero of=/dev/null bs=4k count=200000"),
            timeout_seconds=300,
            repetitions=3,
        ),
        BenchmarkWorkload(
            name="dd_write_512m_fsync",
            description="`dd` 512 MB to /home/user with conv=fsync",
            location=container,
            script=_timed(
                "dd if=/dev/zero of=/home/user/mngr-bench.bin bs=1M count=512 conv=fsync && rm -f /home/user/mngr-bench.bin"
            ),
            timeout_seconds=600,
            repetitions=3,
        ),
        BenchmarkWorkload(
            name="git_clone_flask",
            description="`git clone https://github.com/pallets/flask` (mid-size repo, full history)",
            location=container,
            script=(
                "rm -rf /home/user/mngr-bench-clone\n"
                + _timed("git clone -q https://github.com/pallets/flask /home/user/mngr-bench-clone")
                + "rm -rf /home/user/mngr-bench-clone\n"
            ),
            timeout_seconds=900,
            repetitions=1,
        ),
        BenchmarkWorkload(
            name="uv_sync_cold",
            description="`uv sync --all-packages` with the uv cache and .venv removed first",
            location=container,
            script=(
                'note "uv=$(uv --version 2>/dev/null)"\n'
                "uv cache clean >/dev/null 2>&1; rm -rf /home/user/workspace/.venv\n"
                + _timed("uv sync --all-packages")
            ),
            timeout_seconds=1800,
            repetitions=1,
        ),
        BenchmarkWorkload(
            name="npm_ci_system_interface",
            description="`npm ci` in system/apps/system_interface/frontend (node_modules and npm cache removed first)",
            location=container,
            script=(
                "cd /home/user/workspace/system/apps/system_interface/frontend || exit 96\n"
                'note "node=$(node --version 2>/dev/null) npm=$(npm --version 2>/dev/null)"\n'
                "rm -rf node_modules /var/cache/user/npm\n" + _timed("npm ci --no-audit --no-fund")
            ),
            timeout_seconds=1800,
            repetitions=1,
        ),
        BenchmarkWorkload(
            name="fortress_first_page",
            description="Launch Fortress (Playwright) and load a page, to `about:blank` DOM ready",
            location=container,
            script=(
                f"[ -x {_FORTRESS_BINARY} ] || {{ note 'fortress not installed'; exit 95; }}\n"
                "cat > /tmp/mngr-bench-fortress.py <<'PY'\n"
                "import sys\n"
                "from playwright.sync_api import sync_playwright\n"
                "with sync_playwright() as p:\n"
                f'    browser = p.chromium.launch(executable_path="{_FORTRESS_BINARY}", args=["--no-sandbox"])\n'
                "    page = browser.new_page()\n"
                "    page.goto('data:text/html,<title>bench</title><h1>ok</h1>')\n"
                "    assert page.title() == 'bench'\n"
                "    browser.close()\n"
                "PY\n" + _timed("uv run --offline python /tmp/mngr-bench-fortress.py")
            ),
            timeout_seconds=600,
            repetitions=3,
        ),
        BenchmarkWorkload(
            name="claude_version",
            description="`claude --version`",
            location=container,
            script='note "claude=$(claude --version 2>/dev/null | head -1)"\n' + _timed("claude --version"),
            timeout_seconds=300,
            repetitions=3,
        ),
        BenchmarkWorkload(
            name="claude_prompt_round_trip",
            description="`claude -p` one-turn round trip (needs ANTHROPIC_API_KEY passed to the benchmark)",
            location=container,
            script=(
                '[ -n "${ANTHROPIC_API_KEY:-}" ] || { note "no ANTHROPIC_API_KEY provided; skipped"; exit 94; }\n'
                + _timed("claude -p 'Reply with the single word ok' --output-format text --max-turns 1")
            ),
            timeout_seconds=300,
            repetitions=1,
        ),
        BenchmarkWorkload(
            name="container_ssh_connect_latency",
            description="TCP connect + banner to the container sshd (VM 127.0.0.1:2222), median of 100; notes carry p50/p95 ms",
            location=WorkloadLocation.VM,
            script=(
                "python3 - <<'PY'\n"
                "import socket, statistics, time\n"
                "latencies_ms = []\n"
                "for _ in range(100):\n"
                "    started = time.perf_counter()\n"
                "    sock = socket.create_connection(('127.0.0.1', 2222), timeout=5)\n"
                "    sock.recv(64)\n"
                "    sock.close()\n"
                "    latencies_ms.append((time.perf_counter() - started) * 1000)\n"
                "latencies_ms.sort()\n"
                "print(f'MNGR_BENCH_NOTE=p50_ms={statistics.median(latencies_ms):.2f} p95_ms={latencies_ms[94]:.2f} max_ms={latencies_ms[-1]:.2f}')\n"
                "print(f'MNGR_BENCH_SECONDS={statistics.median(latencies_ms) / 1000:.5f}')\n"
                "print('MNGR_BENCH_EXIT=0')\n"
                "PY\n"
            ),
            timeout_seconds=300,
            repetitions=1,
        ),
        BenchmarkWorkload(
            name="earlyoom_pressure",
            description="A python allocator fills the container until earlyoom sheds it; seconds until the kill, notes carry the shed ledger",
            location=container,
            script=(
                "cat > /tmp/mngr-bench-alloc.py <<'PY'\n"
                "chunks = []\n"
                "# 128 MiB per step, far past any machine size: earlyoom sheds it long before.\n"
                "for _ in range(4096):\n"
                "    chunk = bytearray(128 * 1024 * 1024)\n"
                "    for offset in range(0, len(chunk), 4096):\n"
                "        chunk[offset] = 1\n"
                "    chunks.append(chunk)\n"
                "PY\n"
                f"ledger_before=$(wc -l < {_SHED_LEDGER_PATH} 2>/dev/null || echo 0)\n"
                "note \"earlyoom=$(supervisorctl status earlyoom 2>/dev/null | tr -s ' ')\"\n"
                "note \"meminfo_total_kib=$(awk '/^MemTotal:/{print $2}' /proc/meminfo)\"\n"
                + _timed_until_killed("timeout 300 python3 /tmp/mngr-bench-alloc.py")
                + "sleep 2\n"
                f"tail -n +$(( ledger_before + 1 )) {_SHED_LEDGER_PATH} 2>/dev/null | while read -r line; do "
                'note "shed=$line"; done\n'
            ),
            timeout_seconds=600,
            repetitions=1,
        ),
    )


@pure
def parse_workload_output(stdout: str, exit_code: int) -> tuple[float | None, int | None, tuple[str, ...]]:
    """Extract (seconds, the workload's exit code, notes) from one run's stdout.

    ``exit_code`` is the SSH/docker exec exit code, which only matters when the
    script never printed its own MNGR_BENCH_EXIT (a transport or preflight
    failure); the parsed exit code is None then.
    """
    seconds: float | None = None
    workload_exit: int | None = None
    notes: list[str] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(_SECONDS_PREFIX):
            seconds = float(stripped[len(_SECONDS_PREFIX) :])
        elif stripped.startswith(_EXIT_PREFIX):
            workload_exit = int(stripped[len(_EXIT_PREFIX) :])
        elif stripped.startswith(_NOTE_PREFIX):
            notes.append(stripped[len(_NOTE_PREFIX) :])
        else:
            # Anything the workload's commands printed themselves is not a
            # measurement (their stdout is discarded by the timing bracket,
            # but a preflight line can still reach here).
            pass
    if workload_exit is None and exit_code != 0:
        notes.append(f"transport exit {exit_code}")
    return seconds, workload_exit, tuple(notes)


@pure
def summarize_runs(
    workload: BenchmarkWorkload,
    slice_label: str,
    runs: Sequence[tuple[float | None, int | None, tuple[str, ...]]],
) -> WorkloadMeasurement:
    """Fold the repetitions into one measurement: the median of the successful runs."""
    successful = [seconds for seconds, workload_exit, _notes in runs if seconds is not None and workload_exit == 0]
    notes = tuple(note for _seconds, _exit, run_notes in runs for note in run_notes)
    failed_exits = [workload_exit for _seconds, workload_exit, _notes in runs if workload_exit not in (0, None)]
    if not successful:
        error: str | None = "every run failed" if failed_exits else "no measurement (transport or preflight failure)"
    elif failed_exits:
        error = f"{len(failed_exits)} of {len(runs)} runs failed (exit {failed_exits})"
    else:
        error = None
    return WorkloadMeasurement(
        workload_name=workload.name,
        slice_label=slice_label,
        seconds=statistics.median(successful) if successful else None,
        notes=notes,
        error=error,
    )


def wait_for_slice_services(shell: SliceShellInterface) -> None:
    """Bring the workspace's services up (as the VM's boot autostart would) and wait for env-converge.

    A freshly-leased slice's services agent is stopped (the bake stops it);
    the client's create would normally relaunch it. The benchmark relaunches
    it the way ``minds-outer-autostart.sh`` does and then waits for the
    Fortress binary, which env-converge installs on the first boot (a
    chromium download), so the browser workload has something to launch.
    Raises ``SliceBenchmarkError`` when the services never come up.
    """
    status_rc, status_out, _err = shell.run_in_container("supervisorctl status 2>&1", timeout_seconds=60)
    if status_rc != 0 or "RUNNING" not in status_out:
        logger.info("[{}] services agent is not running; relaunching it", shell.label)
        start_rc, _out, start_err = shell.run_in_container(
            _SERVICES_AGENT_START_SCRIPT, timeout_seconds=_SERVICES_READY_TIMEOUT_SECONDS
        )
        if start_rc != 0:
            raise SliceBenchmarkError(f"[{shell.label}] could not start the services agent: {start_err.strip()}")
    wait_script = (
        f"for _ in $(seq {int(_SERVICES_READY_TIMEOUT_SECONDS // 10)}); do "
        f"if [ -x {_FORTRESS_BINARY} ] && supervisorctl status system_interface 2>/dev/null | grep -q RUNNING; "
        "then exit 0; fi; sleep 10; done; supervisorctl status; exit 1"
    )
    ready_rc, ready_out, ready_err = shell.run_in_container(
        wait_script, timeout_seconds=_SERVICES_READY_TIMEOUT_SECONDS + 60
    )
    if ready_rc != 0:
        raise SliceBenchmarkError(
            f"[{shell.label}] services did not come up within {_SERVICES_READY_TIMEOUT_SECONDS:.0f}s: "
            f"{ready_out.strip()} {ready_err.strip()}"
        )


def _vm_used_mib(shell: SliceShellInterface) -> int:
    rc, out, err = shell.run_on_vm("free -m | awk '/^Mem:/{print $3}'", timeout_seconds=60)
    if rc != 0:
        raise SliceBenchmarkError(f"[{shell.label}] free -m failed: {err.strip()}")
    return int(out.strip())


def read_slice_facts(shell: SliceShellInterface, *, vm_ssh_port: int) -> SliceFacts:
    """The per-slice facts the report leads with (runtime, kernel, cap, tmpfs, memory before the run)."""
    inspect_script = (
        f"{_CONTAINER_ID_LOOKUP}; " + 'docker inspect -f "{{.HostConfig.Runtime}} {{.HostConfig.Memory}}" "$cid"'
    )
    rc, inspect_out, err = shell.run_on_vm(inspect_script, timeout_seconds=60)
    if rc != 0:
        raise SliceBenchmarkError(f"[{shell.label}] docker inspect failed: {err.strip()}")
    runtime, memory_bytes = inspect_out.split()
    # statfs (not findmnt): gVisor's mount table does not list the tmpfs
    # mounts the way the host kernel does, but statfs reports the type.
    container_script = (
        "uname -r; nproc; "
        'for m in /run /tmp; do if [ "$(stat -f -c %T "$m" 2>/dev/null)" = tmpfs ]; then echo "tmpfs:$m"; fi; done'
    )
    rc, container_out, err = shell.run_in_container(container_script, timeout_seconds=60)
    if rc != 0:
        raise SliceBenchmarkError(f"[{shell.label}] container facts failed: {err.strip()}")
    lines = container_out.strip().splitlines()
    return SliceFacts(
        label=shell.label,
        vm_ssh_port=vm_ssh_port,
        container_runtime=runtime,
        container_kernel=lines[0],
        container_memory_bytes=int(memory_bytes),
        container_vcpus=int(lines[1]),
        tmpfs_mounts=tuple(line.removeprefix("tmpfs:") for line in lines[2:] if line.startswith("tmpfs:")),
        vm_memory_used_mib_before=_vm_used_mib(shell),
        vm_memory_used_mib_after=None,
        container_memory_usage_after=None,
    )


def read_slice_footprint_after(shell: SliceShellInterface, facts: SliceFacts) -> SliceFacts:
    """Fill in the post-run memory footprint (VM used, container docker stats)."""
    stats_script = f"{_CONTAINER_ID_LOOKUP}; " + 'docker stats --no-stream --format "{{.MemUsage}}" "$cid"'
    rc, stats_out, err = shell.run_on_vm(stats_script, timeout_seconds=120)
    if rc != 0:
        raise SliceBenchmarkError(f"[{shell.label}] docker stats failed: {err.strip()}")
    return facts.model_copy_update(
        to_update(facts.field_ref().vm_memory_used_mib_after, _vm_used_mib(shell)),
        to_update(facts.field_ref().container_memory_usage_after, stats_out.strip()),
    )


def run_workload(
    shell: SliceShellInterface, workload: BenchmarkWorkload, container_env: Mapping[str, str]
) -> WorkloadMeasurement:
    """Run one workload's repetitions on one slice and fold them into a measurement."""
    runs: list[tuple[float | None, int | None, tuple[str, ...]]] = []
    exports = "".join(f"export {key}={shlex.quote(value)}\n" for key, value in container_env.items())
    for repetition_idx in range(workload.repetitions):
        logger.info("[{}] {} ({}/{})", shell.label, workload.name, repetition_idx + 1, workload.repetitions)
        match workload.location:
            case WorkloadLocation.CONTAINER:
                rc, out, err = shell.run_in_container(
                    _CONTAINER_PRELUDE + exports + workload.script, timeout_seconds=workload.timeout_seconds
                )
            case WorkloadLocation.VM:
                rc, out, err = shell.run_on_vm(workload.script, timeout_seconds=workload.timeout_seconds)
            case _ as unreachable:
                assert_never(unreachable)
        seconds, workload_exit, notes = parse_workload_output(out, rc)
        if workload_exit not in (0, None) or seconds is None:
            logger.warning(
                "[{}] {} run {} failed: rc={} {}", shell.label, workload.name, repetition_idx + 1, rc, err[-500:]
            )
        runs.append((seconds, workload_exit, notes))
    return summarize_runs(workload, shell.label, runs)


@pure
def _format_seconds(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 0.1:
        return f"{seconds * 1000:.1f} ms"
    return f"{seconds:.2f} s"


@pure
def render_report_markdown(report: BenchmarkReport, workloads: Sequence[BenchmarkWorkload]) -> str:
    """The markdown report: per-slice facts, the timing table with the runsc/runc ratio, footprint, notes."""
    labels = [facts.label for facts in report.slices]
    by_key = {(m.workload_name, m.slice_label): m for m in report.measurements}
    lines = [
        f"# runc vs runsc slice benchmark ({report.generated_at:%Y-%m-%d})",
        "",
        f"Box `{report.box_address}`, default-workspace-template ref `{report.dwt_ref}`, generated "
        f"{report.generated_at:%Y-%m-%d %H:%M} UTC by `apps/minds_admin/scripts/slice_runtime_benchmark.py`.",
        "",
        "## Slices",
        "",
        "| label | VM port | container runtime | container kernel | vCPUs | memory cap | tmpfs |",
        "|---|---|---|---|---|---|---|",
    ]
    for facts in report.slices:
        lines.append(
            f"| {facts.label} | {facts.vm_ssh_port} | {facts.container_runtime} | {facts.container_kernel} | "
            f"{facts.container_vcpus} | {facts.container_memory_bytes / 1024**3:.2f} GiB | "
            f"{', '.join(facts.tmpfs_mounts) or 'none'} |"
        )
    lines += ["", "## Timings (median of the repetitions)", ""]
    has_ratio_column = RUNC_LABEL in labels and RUNSC_LABEL in labels
    header_cells = ["workload", *labels] + ([f"{RUNSC_LABEL} / {RUNC_LABEL}"] if has_ratio_column else [])
    lines.append("| " + " | ".join(header_cells) + " |")
    lines.append("|" + "---|" * len(header_cells))
    for workload in workloads:
        cells = [f"`{workload.name}`"]
        for label in labels:
            measurement = by_key.get((workload.name, label))
            cell = _format_seconds(measurement.seconds) if measurement else "-"
            if measurement is not None and measurement.error:
                cell += " (!)"
            cells.append(cell)
        if has_ratio_column:
            runc = by_key.get((workload.name, RUNC_LABEL))
            runsc = by_key.get((workload.name, RUNSC_LABEL))
            if runc and runsc and runc.seconds and runsc.seconds is not None:
                cells.append(f"{runsc.seconds / runc.seconds:.2f}x")
            else:
                cells.append("-")
        lines.append("| " + " | ".join(cells) + " |")
    lines += ["", "(!) marks a workload where some run failed; see the notes.", "", "## Workloads", ""]
    for workload in workloads:
        lines.append(
            f"- `{workload.name}`: {workload.description} ({workload.location.value.lower()}, x{workload.repetitions})"
        )
    lines += [
        "",
        "## Memory footprint",
        "",
        "| label | VM used before | VM used after | container (docker stats) after |",
        "|---|---|---|---|",
    ]
    for facts in report.slices:
        lines.append(
            f"| {facts.label} | {facts.vm_memory_used_mib_before} MiB | "
            f"{'-' if facts.vm_memory_used_mib_after is None else f'{facts.vm_memory_used_mib_after} MiB'} | "
            f"{'-' if facts.container_memory_usage_after is None else facts.container_memory_usage_after} |"
        )
    lines += ["", "## Notes", ""]
    for measurement in report.measurements:
        if measurement.notes or measurement.error:
            lines.append(f"- `{measurement.workload_name}` on {measurement.slice_label}:")
            if measurement.error:
                lines.append(f"  - error: {measurement.error}")
            for note in measurement.notes:
                lines.append(f"  - {note}")
    return "\n".join(lines) + "\n"


def run_benchmark(
    *,
    box_address: str,
    private_key_path: Path,
    vm_ssh_port_by_label: Mapping[str, int],
    workloads: Sequence[BenchmarkWorkload],
    container_env: Mapping[str, str],
    dwt_ref: str,
    generated_at: datetime,
) -> BenchmarkReport:
    """Run every workload on every slice (slice by slice) and assemble the report."""
    # The scratch known_hosts file (ssh creates it at the first accept-new
    # contact) lives in a temp dir that goes away with the run.
    with tempfile.TemporaryDirectory(prefix="mngr-bench-") as scratch_dir:
        known_hosts_path = Path(scratch_dir) / "known_hosts"
        facts_by_label: dict[str, SliceFacts] = {}
        measurements: list[WorkloadMeasurement] = []
        for label, vm_ssh_port in vm_ssh_port_by_label.items():
            shell = SshSliceShell(
                label=label,
                box_address=box_address,
                vm_ssh_port=vm_ssh_port,
                private_key_path=private_key_path,
                known_hosts_path=known_hosts_path,
            )
            logger.info("[{}] waiting for the workspace services on VM port {}", label, vm_ssh_port)
            wait_for_slice_services(shell)
            facts = read_slice_facts(shell, vm_ssh_port=vm_ssh_port)
            logger.info("[{}] runtime={} kernel={}", label, facts.container_runtime, facts.container_kernel)
            for workload in workloads:
                measurements.append(run_workload(shell, workload, container_env))
            facts_by_label[label] = read_slice_footprint_after(shell, facts)
        return BenchmarkReport(
            generated_at=generated_at,
            box_address=box_address,
            dwt_ref=dwt_ref,
            slices=tuple(facts_by_label[label] for label in vm_ssh_port_by_label),
            measurements=tuple(measurements),
        )
