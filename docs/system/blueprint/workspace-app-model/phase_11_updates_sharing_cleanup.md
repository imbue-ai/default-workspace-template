# Phase 11: updates, sharing, and cleanup

Contracts: [contracts.md](contracts.md) sections 3, 14, and 15.

## Goal

Finish the apply, retarget the external callers, document sharing, rename service to app across the shell's code and docs, rewrite the READMEs and the remaining skills, and record the pre-deploy checklist.

## Files

Modified:

- `.agents/skills/update-self/scripts/update_apply.py` and `update_probes.py`: after the restart the probes are `/api/health` on the shell and `GET /_instances` on every app whose manifest in the tree being applied says `critical = true` and `instances = true` (`read_critical_instance_apps`; the chat and the terminal). Each is reached at the manifest's `instances_url` when it declares one, else at the registry row's `url` re-read on every poll (`wait_instances_healthy`), so the probe follows an app that re-registers at the end of its boot; a missing row is "not up yet", and only a 200 with a JSON body counts (a stale row that still names the shell's port gets the shell's SPA catch-all, 200 as HTML). The hard-coded chat port and the shell-origin guard are gone; the chat's pre-flight boot keeps `/api/health`, since `--preflight` runs no agent manager. Snapshot and restore cover the tool directory of every `critical` app the merge reinstalls and both bundles (landed in phases 1 and 10).
- No `supervisorctl reread && supervisorctl update` step (decided 2026-09-07): the apply's `mngr start --restart system-services` kills the services agent's whole tmux session, bootstrap re-runs in its extra window and execs a fresh supervisord that reads the merged `system/supervisord.conf`, so a program the update adds starts on its own; the step would have started it early only for the restart to restart it again.
- `.agents/skills/update-self/scripts/update_probes.py`, `.agents/skills/update-system-interface/scripts/reveal_system_interface.py`: `/api/health`.
- `system/scripts/forward_port.py`: unchanged. `--icon-file` and `--program` stay: a pre-manifest app registers with them for as long as it exists (phase 9 decided both app forms are supported indefinitely).
- `system/services/share_gateway/README.md`: the chat origin under workspace-level grants and the `[services.chat]` narrowing; the grants example gains it.
- `system/apps/system_interface/README.md`: rewritten around the glossary (the Projects section goes; a Model section points at the meta spec), the not-built and staleness sections kept.
- `docs/system/workspace-internals.md`, `system/apps/README.md`, `system/libs/README.md`, `system/services/README.md`, `README.md` (root), `CLAUDE.md`: apps, instances, manifests, tool environments.
- The shell's code and frontend, and the shared `system/libs/workspace_ui` library: `service` becomes `app` in identifiers and comments where it meant an app (`deriveServiceOrigin` is `deriveAppOrigin`, `serviceIconMarkup` is `appIconMarkupByName`, the placeholder page's `serviceOrigin` is `appOrigin`); `AppEntry` is the inventory entry. What stays: the minds embed contract's `serviceName` payload and `SERVICE_NAME_PATTERN` (a vendored wire contract), `HTTP_SERVICE_UNAVAILABLE`, the `system-services` agent, the `system/services/` directory, and the grants file's `[services.<name>]` key.
- `system/services/oom_priority/README.md`: the `priority` lookup and the `chat` band.
- `docs/system/blueprint/workspace-app-model/plan-workspace-app-model.md`: the phases marked done and any drift folded in.

Deleted: `system/apps/system_interface/imbue/system_interface/agent_discovery.py` if any stub remained (it was already gone); nothing under the old `workspace_layout/` directories (a later release).

Kept for the release after this one, with their `# CLEANUP:` comments saying so: the `system/apps/terminal/notify_terminal_session.py` symlink (a running tmux server keeps the hook command it read at start, so the old path must outlive every workspace's next container restart) and `agent.sh` in the terminal's dispatch directory (the chat's terminal back face, which moves into the chat app once every dispatch directory has been rewritten without it).

## Pre-deploy checklist (manual, recorded in the PR)

1. Upgrade a real pre-arc workspace through update-self and confirm projects, tabs, folder paths, and terminal titles survive (phase 9's manual check).
2. Repeat the memory measurement protocol from `docs/system/blueprint/simplify-chat-data-model/`: RSS of the shell and chat processes with a long chat, several chats opened then stopped, one destroyed; record before and after.
3. Share the workspace and confirm a visitor with a workspace-level grant reaches chat, and one with a `[services.files]` grant reaches only files.
4. Run the minds e2e suites against the paired mngr branch ([mngr_side_changes.md](mngr_side_changes.md)).

## Tests

- Apply tests for the per-app probes (`update_self_test.py`: which apps are read off the tree, the manifest's URL over the registry row's, a poll that follows a re-registration, a stale row's HTML refused, the findings when a poll gives up, the rollback path holding the restored tree's apps) and the per-app snapshot and restore; there is no reread step to test.
- A repo-wide ratchet in `system/test_meta_ratchets.py` (`test_prevent_service_identifiers_in_the_shell`) counting identifier tokens containing `service` in the shell package, its frontend, and the shared library, excluding tests, the vendored embed contract, and the tokens listed above, pinned at the residue.

## Changelog entries

Every project's entry is finalized; `system/changelog/mngr-better-chat-app-arc.md` carries the user-facing summary.

## Exit criteria

The full template test suite passes, the changelog gate passes, and the checklist is recorded in the PR description.
