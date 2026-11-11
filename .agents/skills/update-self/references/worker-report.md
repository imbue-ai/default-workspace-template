# Worker: the body of a `done` report (the worker guide's Step 6)

Read this when you write a `done` report. The body gives the lead everything
for its audit, the apply, and the results message. The lead sends back a
report that lacks a section it needs, or either of the two rule records under
Validation.

## The sections

- **Catch-up**, first, only on a `done` that answers a catch-up or a retry
  (`worker-catch-up.md`).
- **What's new**: a digest of the changelog entries.
- **Conflicts**: each one and its resolution. For any side-pick, or claim
  that the kept side subsumes the other, list what the discarded side changed
  and where each change ended up, from the base-diff accounting of your
  conflict triage.
- **Merged vs pulled-in**: which change classes reconciled and which came in
  clean.
- **Merge work per web surface**: "none" or "nontrivial" (with a sentence on
  what was reconciled) for the shell and each user web service. The lead
  attaches the rollback offer to each nontrivial one.
- **Customization survival**: each touched creation classified per Step 4b,
  with evidence paths for anything not plainly intact, and the scope-file
  evidence for the manifest apps: which carried local content and which the
  update reached.
- **Built frontend bundles**: when you built them, the absolute paths of all
  seven; omit the section when you did not build.
  - `<your work_dir>/system/apps/system_interface/imbue/system_interface/static`
  - `<your work_dir>/system/apps/chat/imbue/chat/static`
  - `<your work_dir>/system/apps/getting_started/src/getting_started/static`
  - `<your work_dir>/system/apps/files/static`
  - `<your work_dir>/system/apps/routines/src/routines_app/static`
  - `<your work_dir>/system/apps/screen/src/screen/static`
  - `<your work_dir>/system/apps/style_guide/src/style_guide_app/static`
- **Impact analysis**: what you checked and how; the retired names you
  enumerated and what each search found; each use you rewrote to a stated
  replacement; each hit you report instead; and any user-created app or skill
  depending on a changed file. When the Step 4a rule skipped the analysis:
  the footprint evidence (`has_local_footprint: false` and the `local_only`
  paths) and what the routine check found.
- **Dockerfile split** (if it merged): each hunk live-applicable or
  image-level. Version pins live in `setup_system.sh`, so a pin bump is a
  provisioner change, not a Dockerfile hunk. Mark each image-level hunk by
  whether a fresh image would give the user anything the applied workspace
  lacks:
  - **covered live**: nothing. Its effect already reaches the running
    container another way (the merged tree the image only copies in, an
    installer `setup_system.sh` runs from the repo on the apply's re-run, a
    removed install that nothing in the merged tree runs any more), or it
    only shapes the build itself (a layer-cache `COPY`). Say which.
  - **pending recreate**: something. A base `FROM` change, a package or
    build-time path nothing outside the image build provides. Say what the
    running workspace lacks, or still has, until a recreate.
- **Provisioning changes** and **global-dependency bumps** (if any), per
  `worker-provisioning-changes.md`: each classified live-applicable (naming
  the in-branch mirror edits you made) or rebuild-only, with the version
  delta. A user-created dependent of a pin bump is "applied live, unverified
  in the worktree; the lead checks it after the apply", with what your
  research turned up. Only a change whose absence would break the running
  workspace is a `stuck` report.
- **Validation**:
  - **Which branch of the 4b scope rule applied, with its evidence**: each
    item's condition and whether it held; on a clean pull with no footprint,
    that only the skill-exemption stamp ran, and why.
  - The stamp's output lines.
  - The suites, boots and Playwright that did run, all passing.
  - Any test that failed and then passed when rerun alone, here or after a 4c
    commit, as flaky.
  - Each failing test's attribution: caused by the update, or pre-existing.
  - **Which branch of the 4c rule applied, with its evidence**: the
    clean-pull skip's three conditions, or the gate run's kept and reverted
    fix commits (or "gate ran clean") and the architecture-gate verdicts.
  - Any validation gap, called out honestly.
