# Unattended workspace updates

The plan for this feature lives in the mngr repo, beside the Imbue Studio app that schedules and
dispatches the updates: `blueprint/unattended-workspace-updates/` there, with
`plan-unattended-workspace-updates.md` (what the feature does, in both repos) and `contract.md`
(the files the app writes inside a workspace, the dispatch message, and the run record fields
this template adds).

This template's half is the run itself: the update-self skill's licence to pause chats and save a
dirty tree, the pinned target in Step 2, the needs-decision ending, the apply-time pause and
resume (`run-status stop-chats` / `resume-chats`), the chat's "Workspace updated" notice, the
chat root's `open=providers` key, and the update records in bug reports.
