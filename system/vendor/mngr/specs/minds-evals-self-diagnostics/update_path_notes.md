# Mid-trial changes through the update path: notes from the first cut

**Status:** notes, not a spec. Not part of the self-diagnostic suite (`spec.md`), and not built by its milestones. This records how a later case could land a change through the product's own update machinery: a case where that machinery is what is evaluated, or one where a change arrives between steps. It was worked out, and partly verified in code, while designing the seed, on default-workspace-template `main@96935db5`.

## How production lands a change

Read in default-workspace-template at `main@96935db5`; `minds-v0.5.2` has the same `apply --merge-ref` entry point, and differences in its internals are unexamined.

- Minds never pushes git into a running workspace. `update_service.dispatch_update` (`apps/minds`) starts a chat whose first message is `/update-self`. That skill launches a worker agent to merge the target on a branch, and the lead then lands the branch with `python3 .agents/skills/update-self/scripts/update_self.py apply --merge-ref <ref> --ff-only --target-ref <tag>`, run from a staged copy of the skill.
- `apply` itself starts no agent and fetches nothing: it runs `git merge` on a ref that must already resolve in the workspace repo, so a tag serves as well as a branch.
- It refuses a dirty tree, merges (a conflicting merge is aborted with exit `1` and nothing changed), and plans from the diff: `uv tool install -e` for every touched app with a `pyproject.toml` and an `app.toml`, a reinstall of the root venv and every app tool when a backend manifest such as `uv.lock` changed, and a re-run of `setup_system.sh` when its inputs changed.
- Whatever the diff, it pre-flights `system-interface` and `chat-app`, removes shadowing `mngr` installs, runs the layout migration, restarts `system-services` (whose fresh supervisord reads the merged `system/supervisord.conf`), and probes the shell, the frontend, the chat app and the terminal. A pre-flight or probe failure rolls back.
- Exit codes: `0` applied, `1` a precondition failed and nothing changed, `2` rolled back, `3` emergency.
- The restart's bootstrap runs `update_self.py recover --if-stale --grace-seconds 0`, which rolls back any apply whose marker names a process that is no longer alive.
- `update-system-interface` already calls `apply` on a local, non-release branch without `--ff-only` or `--target-ref`, which is the precedent for an eval.

## How a step would land one

1. **Build before launch.** The change is authored like a seed, as a commit on `dwt_sha`, and tagged `eval-update-<step>` in the case clone. Minds' plain `git clone` of the case clone copies tags, and `mngr create` pushes `refs/tags/*` into the workspace, so the tag is there from create onwards. A branch would not be, since only the checked-out branch is a local head in that clone. Checked with plain git; the Minds and Modal path is unverified. The driver cannot push later instead: `mngr git push` pushes the repository it runs in, and inside the clone it loads the template's project config, which disables the Modal provider.
2. **Conflicts.** A conflict with the template is found at build time, as for a seed; a conflict with the agent's own commits can only surface in the workspace, where `apply` exits `1` and changes nothing, and the step records the change as impossible at that point.
3. **Apply between steps, detached.** After the previous step's evidence phase and before the step's first message, the driver checks through `minds_bridge.run_in_workspace` that `git status --porcelain` is empty, takes a registry snapshot, and launches:

   ```sh
   mkdir -p /tmp/eval-update && env -u MNGR_AGENT_ID MNGR_AGENT_NAME=eval-update PATH=<the recovery cron's PATH> \
     setsid -f sh -c 'python3 .agents/skills/update-self/scripts/update_self.py apply --merge-ref eval-update-<step> \
       > /tmp/eval-update/apply.log 2>&1; echo $? > /tmp/eval-update/exit_code' </dev/null >/dev/null 2>&1
   ```

   Each part of that line is load-bearing:
   - `run_in_workspace` runs through `mngr exec`, which sources the target agent's environment, and `apply`'s restart kills every process carrying that agent's `MNGR_AGENT_ID` (`Host._collect_pids_by_agent_id_env`). A foreground `apply` would kill itself between the stop and start halves of its own restart, and bootstrap would then roll it back. `env -u` ahead of `setsid -f sh -c` removes the marker from the whole detached tree, including the shell that writes the exit code.
   - The redirections release the exec's channel, so `mngr exec` returns at once instead of waiting for end-of-file until its timeout.
   - `apply` shells out to `mngr`, `uv` and `npm`, and a missing one on `PATH` is a rollback, so the command carries the `PATH` the template's recovery cron sets for the same reason (`bootstrap/manager.py`).
   - The log and the exit file live under `/tmp`, which survives a supervisord restart and cannot dirty the tree.
4. **Verify.** The driver polls the exit file with short execs that tolerate the restart window, then verifies whatever the change registers, since `apply`'s probes never look at a user program. A second registry snapshot, against the first, gives the rows the change brought: measured, not declared.

The restart stops and starts every `system-services` program, the chat app's server included.
The chat agent is its own mngr agent, whose processes the restart's `MNGR_AGENT_ID` sweep does not match, so the conversation should survive it; that is inferred, and it is the first thing such a case's first run has to show.

This departs from production in stated ways: the driver runs `apply` rather than a chat agent does, it runs the checked-out script rather than a staged copy, it merges a tag, and it passes neither `--ff-only` (the workspace's `main` carries the agent's commits) nor `--target-ref` (the change is not a release and must write no version history).

