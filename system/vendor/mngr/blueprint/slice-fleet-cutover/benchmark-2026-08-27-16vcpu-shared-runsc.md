# runc vs runsc slice benchmark (2026-08-27)

Box `51.81.208.81`, default-workspace-template ref `new-fleet-runsc-prototype`, generated 2026-08-27 14:07 UTC by `apps/minds_admin/scripts/slice_runtime_benchmark.py`.

## Slices

| label | VM port | container runtime | container kernel | vCPUs | memory cap | tmpfs |
|---|---|---|---|---|---|---|
| runsc | 22004 | runsc | 4.19.0-gvisor | 16 | 6.75 GiB | /run, /tmp |

## Timings (median of the repetitions)

| workload | runsc |
|---|---|
| `python_startup_x20` | 0.37 s |
| `node_startup_x20` | 0.89 s |
| `find_workspace` | 0.78 s |
| `git_status` | 0.32 s |
| `compileall_mngr` | 1.94 s |
| `syscall_loop_dd` | 0.93 s |
| `dd_write_512m_fsync` | 0.75 s |
| `uv_sync_cold` | 7.48 s |
| `claude_version` | 0.13 s |

(!) marks a workload where some run failed; see the notes.

## Workloads

- `python_startup_x20`: `python3 -c pass` x20 (container, x3)
- `node_startup_x20`: `node -e 0` x20 (container, x3)
- `find_workspace`: `find /home/user/workspace -type f | wc -l` (container, x3)
- `git_status`: `git status --porcelain` in the workspace (container, x3)
- `compileall_mngr`: `python3 -m compileall -f` over the vendored libs/mngr (container, x3)
- `syscall_loop_dd`: `dd if=/dev/zero of=/dev/null bs=4k count=200000` (syscall loop) (container, x3)
- `dd_write_512m_fsync`: `dd` 512 MB to /home/user with conv=fsync (container, x3)
- `uv_sync_cold`: `uv sync --all-packages` with the uv cache and .venv removed first (container, x1)
- `claude_version`: `claude --version` (container, x3)

## Memory footprint

| label | VM used before | VM used after | container (docker stats) after |
|---|---|---|---|
| runsc | 1304 MiB | 1632 MiB | 1.167GiB / 6.754GiB |

## Notes

- `uv_sync_cold` on runsc:
  - uv=uv 0.11.7 (x86_64-unknown-linux-gnu)
- `claude_version` on runsc:
  - claude=2.1.227 (Claude Code)
  - claude=2.1.227 (Claude Code)
  - claude=2.1.227 (Claude Code)
