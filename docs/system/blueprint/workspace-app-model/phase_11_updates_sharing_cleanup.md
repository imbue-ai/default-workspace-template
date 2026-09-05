# Phase 11: updates, sharing, and cleanup

Contracts: [contracts.md](contracts.md) sections 3, 14, and 15.

## Goal

Finish the apply, retarget the external callers, document sharing, rename service to app across the shell's code and docs, rewrite the READMEs and the remaining skills, and record the pre-deploy checklist.

## Files

Modified:

- `.agents/skills/update-self/scripts/update_apply.py` and `update_environment.py`: `supervisorctl reread && supervisorctl update` after the merge and before the health probes; the health probes use `/api/health` on the shell and `GET /_instances` on every `critical` app with `instances = true`; snapshot and restore cover the tool directory and bundle of every `critical` app.
- `.agents/skills/update-self/scripts/update_probes.py`, `.agents/skills/update-system-interface/scripts/reveal_system_interface.py`: `/api/health`.
- `system/scripts/forward_port.py`: `--icon-file` and `--program` removed; every remaining caller (owner-exec, vm-exec, previews, isolated test servers) uses `--name --url` with `--internal` or `--no-icon`.
- `system/services/share_gateway/README.md`: the chat origin under workspace-level grants and the `[services.chat]` narrowing; the grants example gains it.
- `system/apps/system_interface/README.md`: rewritten around the glossary (the Projects section goes; a Model section points at the meta spec), the not-built and staleness sections kept.
- `docs/system/workspace-internals.md`, `system/apps/README.md`, `system/libs/README.md`, `system/services/README.md`, `README.md` (root), `CLAUDE.md`: apps, instances, manifests, tool environments.
- The shell's code and frontend: `service` becomes `app` in identifiers and comments where it meant an app; `AppEntry` is the inventory entry; `serviceName` becomes `appName`.
- `system/services/oom_priority/README.md`: the `priority` lookup and the `chat` band.
- `docs/system/blueprint/workspace-app-model/plan-workspace-app-model.md`: the phases marked done and any drift folded in.

Deleted: `system/apps/system_interface/imbue/system_interface/agent_discovery.py` if any stub remained; nothing under the old `workspace_layout/` directories (a later release).

## Pre-deploy checklist (manual, recorded in the PR)

1. Upgrade a real pre-arc workspace through update-self and confirm projects, tabs, folder paths, and terminal titles survive (phase 9's manual check).
2. Repeat the memory measurement protocol from `docs/system/blueprint/simplify-chat-data-model/`: RSS of the shell and chat processes with a long chat, several chats opened then stopped, one destroyed; record before and after.
3. Share the workspace and confirm a visitor with a workspace-level grant reaches chat, and one with a `[services.files]` grant reaches only files.
4. Run the minds e2e suites against the paired mngr branch ([mngr_side_changes.md](mngr_side_changes.md)).

## Tests

- Apply tests for the reread step, the per-app probes, and the per-app snapshot and restore.
- `forward_port_test.py` for the removed flags.
- A repo-wide ratchet in `system/test_meta_ratchets.py` counting `service` in shell identifiers, set to the residue and never rising.

## Changelog entries

Every project's entry is finalized; `system/changelog/mngr-better-chat-app-arc.md` carries the user-facing summary.

## Exit criteria

The full template test suite passes, the changelog gate passes, and the checklist is recorded in the PR description.
