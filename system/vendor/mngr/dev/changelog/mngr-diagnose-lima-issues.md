Added the blueprint planning document for the Lima workspace reliability work (`blueprint/lima-workspace-reliability/plan-lima-workspace-reliability.md`), covering the fixes from the mngr-internal#121 investigation and the planned in-flight-creation UX.

Updated `uv.lock` and the public-mirror `mirror/overlay/uv.lock` for the new `tenacity` dependency of `mngr_lima` (used to retry `limactl start` on transient image-download failures).
