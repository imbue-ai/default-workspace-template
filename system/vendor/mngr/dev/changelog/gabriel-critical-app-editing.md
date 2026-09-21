Adds the mngr-side spec for editing the workspace's critical apps
(`blueprint/critical-app-editing/plan-critical-app-editing.md`). It names what
this branch changes in mngr, why the paired default-workspace-template branch
needs it first, and points at that repo's spec for the rest. This branch merges
the unmerged `gabriel/denim-pigeon` branch's work, whose changelog entries it
carries under each project it touched, and adds the follower's start-without-a-
writer mode on top.

Also adds `blueprint/critical-app-editing/retrospective-harden-worker-update-chat-theme-controls.md`, a retrospective on the 3-hour harden worker run observed in the `criticaltest` staging workspace on this branch pair. It records that the worker's sleep-polling, whole-tree test scope and OOM sheds, and the lead's failure to consume the finished report, all come from main's worker and lead guidance and the lead's 15-second idle detector, not from this branch, and lists the constraints a fix has to satisfy.
