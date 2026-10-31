# Root environment migration and launcher identity

An inexact `uv run` can keep an obsolete distribution while adding its renamed
replacement. When both RECORDs own the same runtime paths, a later exact sync
can uninstall the old owner and delete files the retained replacement still
claims. A second sync may exit successfully without restoring those files.
This happened for `overlay` → `imbue-overlay` (eight `imbue/overlay` modules),
and reproduces with uv 0.11.7 and 0.13.4. It is a built-in update-flow defect.

The backend refresh uses `uv sync --all-packages --frozen --reinstall` in the
root environment's final location. Reinstalling the exact locked closure
handles both overlapping installed owners and a previously hollow installation.
It preserves the interpreter and prefix, snapshot/rollback flow, and exact pins.
It costs additional installation work and can require access to package sources;
the existing refresh timeout and official rollback handle failure. A targeted
`--reinstall-package imbue-overlay` repairs this specific rename but cannot
establish integrity after other installed files disappeared. A clean rebuild is
appropriate only when reinstall cannot restore integrity: retain the old state,
select the tree's supported interpreter explicitly, build at the final prefix,
and use official snapshots/recovery. Never copy a worker environment into live.

Prevention complements repair: quiesce all root-environment writers (including
spawned installers, not merely chats) through the snapshot/apply/rollback window.
Use system Python or `uv run --no-sync` for inspection. An arbitrary independent
writer does not participate in the update lock; this is an executor coordination
requirement, not an assurance that the apply can lock out all other processes.
Do not accept a racing snapshot as proof of a complete saved environment.

## Custom launcher contract

Use the official `update_self.py` entrypoint whenever possible. Any wrapper
that calls its apply/recover implementation **must retain `update_self` in its
Linux process command line**, conventionally in its filename (for example,
`update_self_clean_environment.py`). Both old and current recovery contracts
check a live PID and this token to reject reused PIDs. A wrapper filename that
omits the token is an integration oversight: unattended recovery can mistake
its still-running apply for a stale process and roll back underneath it.
Do not weaken PID reuse protection to accommodate an incorrectly named wrapper.
Before using a wrapper, test its real child command line against the deployed
and staged liveness detectors and run a separate official stale-recovery command
against a private fixture while the child stays alive. Dead applies and unrelated
live PIDs must still permit recovery. Never validate this contract with live apply.
