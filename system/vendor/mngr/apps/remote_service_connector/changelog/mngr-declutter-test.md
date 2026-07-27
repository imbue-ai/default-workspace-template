Integration branch combining the user-data-layout trains (`mngr/fix-data-layout`, `mngr/declutter-template`) with `mngr/fix-apt-mirror`; the full per-train details live in this project's sibling entries for those branches.

For this project: the snapshot-pinned apt mirror briefly hosted in the connector on this PR train moved out before ever deploying -- it now lives in the standalone `apps/apt_mirror` project, and the connector's `app.py` is again fully self-contained (no `boto3`/`imbue-common`/`loguru` image dependencies).
