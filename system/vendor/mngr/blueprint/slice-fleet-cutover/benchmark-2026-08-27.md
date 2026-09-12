# runc vs runsc slice benchmark (2026-08-27)

Box `51.81.208.81`, default-workspace-template ref `new-fleet-runsc-prototype`, generated 2026-08-27 04:22 UTC by `apps/minds_admin/scripts/slice_runtime_benchmark.py`.

## Slices

| label | VM port | container runtime | container kernel | vCPUs | memory cap | tmpfs |
|---|---|---|---|---|---|---|
| runc | 22002 | runc | 6.12.96+deb13-cloud-amd64 | 2 | 7.00 GiB | /run, /tmp |
| runsc | 22004 | runsc | 4.19.0-gvisor | 2 | 7.00 GiB | /run, /tmp |

## Timings (median of the repetitions)

| workload | runc | runsc | runsc / runc |
|---|---|---|---|
| `python_startup_x20` | 0.22 s | 0.69 s | 3.16x |
| `node_startup_x20` | 0.54 s | 1.58 s | 2.94x |
| `find_workspace` | 63.6 ms | 2.24 s | 35.16x |
| `tar_workspace` | 0.11 s | 0.71 s | 6.26x |
| `git_status` | 41.8 ms | 0.28 s | 6.58x |
| `compileall_mngr` | 0.82 s | 1.25 s | 1.52x |
| `syscall_loop_dd` | 80.6 ms | 1.16 s | 14.45x |
| `dd_write_512m_fsync` | 0.64 s | 0.69 s | 1.07x |
| `git_clone_flask` | 1.18 s | 2.28 s | 1.93x |
| `uv_sync_cold` | 2.71 s | 11.40 s | 4.20x |
| `npm_ci_system_interface` | 2.57 s | 4.82 s | 1.87x |
| `fortress_first_page` | 1.04 s | 2.53 s | 2.44x |
| `claude_version` | 82.4 ms | 0.11 s | 1.36x |
| `claude_prompt_round_trip` | 30.05 s | 11.26 s | 0.37x |
| `container_ssh_connect_latency` | 0.1 ms | 0.4 ms | 2.40x |
| `earlyoom_pressure` | 1.38 s | 3.41 s | 2.47x |

(!) marks a workload where some run failed; see the notes. The `claude_prompt_round_trip` and `earlyoom_pressure` rows come from a second pass of only those two workloads (the first pass's script bugs are noted in `checkpoint-2026-08-27.md`); `claude_prompt_round_trip` is one network round trip through the LiteLLM proxy and says nothing about the runtime; `earlyoom_pressure` is the seconds an allocator survived until earlyoom shed it (`command_exit=143` = SIGTERM from earlyoom).

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
| runc | 1613 MiB | 1517 MiB | 925.9MiB / 7GiB |
| runsc | 1901 MiB | 1677 MiB | 1.045GiB / 7GiB |

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
  - p50_ms=0.15 p95_ms=4.60 max_ms=6.88
- `earlyoom_pressure` on runc:
  - earlyoom=earlyoom RUNNING pid 9334, uptime 0:30:40
  - meminfo_total_kib=8134692
  - command_exit=143
  - shed={"timestamp": "2026-08-27T04:53:02.056986Z", "type": "process_shed", "pid": 39727, "comm": "python3", "agent_name": null, "is_worker": null}
- `uv_sync_cold` on runsc:
  - uv=uv 0.11.7 (x86_64-unknown-linux-gnu)
- `npm_ci_system_interface` on runsc:
  - node=v22.23.2 npm=10.9.8
- `claude_version` on runsc:
  - claude=2.1.227 (Claude Code)
  - claude=2.1.227 (Claude Code)
  - claude=2.1.227 (Claude Code)
- `container_ssh_connect_latency` on runsc:
  - p50_ms=0.36 p95_ms=24.26 max_ms=38.28
- `earlyoom_pressure` on runsc:
  - earlyoom=earlyoom RUNNING pid 9760, uptime 0:24:44
  - meminfo_total_kib=7340032
  - command_exit=143
  - shed={"timestamp": "2026-08-27T04:53:27.093125Z", "type": "process_shed", "pid": 34253, "comm": "python3", "agent_name": null, "is_worker": null}
