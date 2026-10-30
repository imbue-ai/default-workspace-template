"""Tests for register_vm_exec.sh's default-gateway resolution on a container without iproute2."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

_SCRIPT = Path(__file__).parent / "register_vm_exec.sh"
_ROUTE_TABLE_HEADER = (
    "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
)
# The route table of a slice container: the docker bridge's subnet, then the
# default route through the VM side of the bridge (172.17.0.1).
_SLICE_ROUTE_TABLE = (
    _ROUTE_TABLE_HEADER
    + "eth0\t000011AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
    + "eth0\t00000000\t010011AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
)
_NO_DEFAULT_ROUTE_TABLE = (
    _ROUTE_TABLE_HEADER + "eth0\t000011AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
)


def _bin_dir_without_ip(tmp_path: Path) -> Path:
    """A PATH of just the tools the script needs, with no `ip` and a curl that always fails."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("tail", "awk"):
        resolved = shutil.which(tool)
        assert resolved is not None
        (bin_dir / tool).symlink_to(resolved)
    curl = bin_dir / "curl"
    curl.write_text("#!/bin/sh\nexit 7\n")
    curl.chmod(0o755)
    return bin_dir


def _run_script(tmp_path: Path, route_table: str) -> subprocess.CompletedProcess[str]:
    route_table_path = tmp_path / "route"
    route_table_path.write_text(route_table)
    bash = shutil.which("bash")
    assert bash is not None
    return subprocess.run(
        [bash, str(_SCRIPT)],
        env={
            "PATH": str(_bin_dir_without_ip(tmp_path)),
            "HOME": str(tmp_path),
            "MINDS_WORKSPACE_ROOT": str(tmp_path),
            "REGISTER_VM_EXEC_ROUTE_TABLE": str(route_table_path),
        },
        capture_output=True,
        text=True,
        check=False,
        cwd=os.fspath(tmp_path),
    )


def test_gateway_is_read_from_the_route_table_when_ip_is_absent(tmp_path: Path) -> None:
    result = _run_script(tmp_path, _SLICE_ROUTE_TABLE)

    assert result.returncode == 0
    assert "no vm-exec at http://172.17.0.1:8794/_alive" in result.stderr


def test_a_route_table_without_a_default_route_skips_registration(
    tmp_path: Path,
) -> None:
    result = _run_script(tmp_path, _NO_DEFAULT_ROUTE_TABLE)

    assert result.returncode == 0
    assert "no default gateway; skipping vm-exec registration" in result.stderr
