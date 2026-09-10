# runc vs runsc slice benchmark (2026-08-27)

Box `51.81.208.81`, default-workspace-template ref `new-fleet-runsc-prototype`, generated 2026-08-27 13:49 UTC by `apps/minds_admin/scripts/slice_runtime_benchmark.py`.

## Slices

| label | VM port | container runtime | container kernel | vCPUs | memory cap | tmpfs |
|---|---|---|---|---|---|---|
| runc | 22002 | runc | 6.12.105+deb13-cloud-amd64 | 16 | 6.75 GiB | /run, /tmp |
| runsc | 22004 | runsc | 4.19.0-gvisor | 16 | 6.75 GiB | /run, /tmp |

## Timings (median of the repetitions)

| workload | runc | runsc | runsc / runc |
|---|---|---|---|
| `python_startup_x20` | 0.18 s | 0.30 s | 1.64x |
| `node_startup_x20` | 0.40 s | 1.24 s | 3.06x |
| `find_workspace` | 1.4 ms | 18.9 ms | 13.50x |
| `tar_workspace` | 0.11 s | 0.25 s | 2.22x |
| `git_status` | 37.9 ms | 98.3 ms | 2.59x |
| `compileall_mngr` | 0.84 s | 1.84 s | 2.19x |
| `syscall_loop_dd` | 82.8 ms | 0.95 s | 11.42x |
| `dd_write_512m_fsync` | 0.65 s | 0.84 s | 1.28x |
| `git_clone_flask` | 0.97 s | 1.72 s | 1.78x |
| `uv_sync_cold` | 1.88 s | 4.54 s | 2.42x |
| `npm_ci_system_interface` | 2.13 s | 3.79 s | 1.78x |
| `fortress_first_page` | 0.91 s | 2.18 s | 2.40x |
| `claude_version` | 81.5 ms | 0.21 s | 2.63x |
| `claude_prompt_round_trip` | 32.60 s | 13.64 s | 0.42x |
| `container_ssh_connect_latency` | 0.1 ms | 0.4 ms | 2.57x |
| `earlyoom_pressure` | 1.66 s | 3.77 s | 2.27x |

(!) marks a workload where some run failed; see the notes.

## Workloads

- `python_startup_x20`: `python3 -c pass` x20 (container, x3)
- `node_startup_x20`: `node -e 0` x20 (container, x3)
- `find_workspace`: `find /home/user/workspace -type f | wc -l` (container, x3)
- `tar_workspace`: `tar -cf /dev/null workspace` (container, x3)
- `git_status`: `git status --porcelain` in the workspace (container, x3)
- `compileall_mngr`: `python3 -m compileall -f` over the vendored libs/mngr (container, x3)
- `syscall_loop_dd`: `dd if=/dev/zero of=/dev/null bs=4k count=200000` (syscall loop) (container, x3)
- `dd_write_512m_fsync`: `dd` 512 MB to /home/user with conv=fsync (container, x3)
- `git_clone_flask`: `git clone https://github.com/pallets/flask` (mid-size repo, full history) (container, x1)
- `uv_sync_cold`: `uv sync --all-packages` with the uv cache and .venv removed first (container, x1)
- `npm_ci_system_interface`: `npm ci` in system/apps/system_interface/frontend (node_modules and npm cache removed first) (container, x1)
- `fortress_first_page`: Launch Fortress (Playwright) and load a page, to `about:blank` DOM ready (container, x3)
- `claude_version`: `claude --version` (container, x3)
- `claude_prompt_round_trip`: `claude -p` one-turn round trip (needs ANTHROPIC_API_KEY passed to the benchmark) (container, x1)
- `container_ssh_connect_latency`: TCP connect + banner to the container sshd (VM 127.0.0.1:2222), median of 100; notes carry p50/p95 ms (vm, x1)
- `earlyoom_pressure`: A python allocator fills the container until earlyoom sheds it; seconds until the kill, notes carry the shed ledger (container, x1)

## Memory footprint

| label | VM used before | VM used after | container (docker stats) after |
|---|---|---|---|
| runc | 1251 MiB | 1273 MiB | 806.5MiB / 6.754GiB |
| runsc | 1488 MiB | 1884 MiB | 1.254GiB / 6.754GiB |

## Notes

- `uv_sync_cold` on runc:
  - uv=uv 0.11.7 (x86_64-unknown-linux-gnu)
- `npm_ci_system_interface` on runc:
  - node=v22.23.2 npm=10.9.8
- `claude_version` on runc:
  - claude=2.1.227 (Claude Code)
  - claude=2.1.227 (Claude Code)
  - claude=2.1.227 (Claude Code)
- `container_ssh_connect_latency` on runc:
  - p50_ms=0.14 p95_ms=4.67 max_ms=7.55
- `earlyoom_pressure` on runc:
  - earlyoom=earlyoom RUNNING pid 175, uptime 0:28:18
  - meminfo_total_kib=8130868
  - command_exit=143
  - shed={"timestamp": "2026-08-27T13:56:00.360018Z", "type": "process_shed", "pid": 3834, "comm": "python3", "agent_name": null, "is_worker": null}
- `uv_sync_cold` on runsc:
  - uv=uv 0.11.7 (x86_64-unknown-linux-gnu)
- `npm_ci_system_interface` on runsc:
  - node=v22.23.2 npm=10.9.8
- `claude_version` on runsc:
  - claude=2.1.227 (Claude Code)
  - claude=2.1.227 (Claude Code)
  - claude=2.1.227 (Claude Code)
- `container_ssh_connect_latency` on runsc:
  - p50_ms=0.36 p95_ms=17.60 max_ms=24.72
- `earlyoom_pressure` on runsc:
  - earlyoom=earlyoom RUNNING pid 193, uptime 0:16:43
  - meminfo_total_kib=7081984
  - command_exit=143
  - shed={"timestamp": "2026-08-27T14:02:29.494271Z", "type": "process_shed", "pid": 3430, "comm": "python3", "agent_name": null, "is_worker": null}
