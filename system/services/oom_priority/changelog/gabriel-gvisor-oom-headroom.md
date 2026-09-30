The OOM drill now predicts the victim the way earlyoom v1.9.0-imbue.3 picks it under gVisor. It counts each process's smaps `Anonymous:` total in place of `VmRSS`, since gVisor's `VmRSS` counts whole mapped ranges (every claude process carries the whole claude binary). It also treats a `VmSize` of 0, gVisor's zombie or exiting task, as a process earlyoom cannot pick. The README and `bands.py` describe the counted memory, including what stays approximate.

The README now describes `--host-meminfo`, which the workspace's earlyoom runs with from this release on.
