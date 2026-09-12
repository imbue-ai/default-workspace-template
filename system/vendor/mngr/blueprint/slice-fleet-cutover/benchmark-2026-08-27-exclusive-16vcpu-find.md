# runc vs runsc slice benchmark (2026-08-27)

Box `51.81.208.81`, default-workspace-template ref `new-fleet-runsc-prototype`, generated 2026-08-27 14:06 UTC by `apps/minds_admin/scripts/slice_runtime_benchmark.py`.

## Slices

| label | VM port | container runtime | container kernel | vCPUs | memory cap | tmpfs |
|---|---|---|---|---|---|---|
| runc | 22002 | runc | 6.12.105+deb13-cloud-amd64 | 16 | 6.75 GiB | /run, /tmp |
| runsc | 22004 | runsc | 4.19.0-gvisor | 16 | 6.75 GiB | /run, /tmp |

## Timings (median of the repetitions)

| workload | runc | runsc | runsc / runc |
|---|---|---|---|
| `find_workspace` | 63.6 ms | 0.26 s | 4.10x |

(!) marks a workload where some run failed; see the notes.

## Workloads

- `find_workspace`: `find /home/user/workspace -type f | wc -l` (container, x3)

## Memory footprint

| label | VM used before | VM used after | container (docker stats) after |
|---|---|---|---|
| runc | 1277 MiB | 1292 MiB | 846.7MiB / 6.754GiB |
| runsc | 1942 MiB | 1920 MiB | 1.331GiB / 6.754GiB |

## Notes

