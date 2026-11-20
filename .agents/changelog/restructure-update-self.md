`update-self` is restructured: SKILL.md is the lead's checklist, and each step's detail is in a reference the step names. No command of the update changed, apart from one teardown step that is gone.

- **SKILL.md is 13.5 KB** (was 54.8 KB): standing instructions, a numbered step list with one entry per step, then each step in a few lines. The launch, the question gates, the audit, the apply, the report-driven remainder and teardown each moved to a reference. The step headings are unchanged, so a lead arriving from an older release still enters at "Step 3".

- **`references/older-workspaces.md`** (new) holds what the lead does differently on a workspace older than some release, and `references/worker/older-workspaces.md` what the worker does. The step that meets such a difference states the test for it, as something the agent can observe and not a version, and points at its section in one line. Each section ends with a `CLEANUP:` line. What holds on every pass because older launchers and older copies of Steps 1-2 exist stays in `references/handoff-contract.md`, which now lists what can go as the floor rises. `heal-shallow-history.md` is folded into them.

- **Teardown no longer tears down a preview of a critical app.** No pass of this skill has stood one up since minds-v0.4.3, and the `<name>-preview` it looked for is the `update-app` skill's careful flow's, which is that flow's to tear down.

- **The worker guide is a core plus rare-path references**: `references/worker.md` (was `update-self-worker.md`), and the files only the worker follows in `references/worker/`. Conflict triage, the impact analysis, the provisioning classes, the validation items, the review gates, the `done` report and the catch-up each have a file there, and the worker's task file tells it to re-read the guide after a compaction.

- **The impact analysis covers every name an update retires.** `update_self.py retired-names --base <ref> --target <ref>` (new) lists removed `[project.scripts]` commands and removed or renamed skill directories as JSON. The worker also reads the template's and mngr's changelog entries for removed flags, environment variables, ports and routes, and searches the user's creations for each name. A use with a stated replacement is rewritten in its branch; any other hit is reported.

- **One home each** for the ceiling's rules, what the pass does on each exit of the apply (what an exit means for any caller is the shared `apply-exit-codes.md`, read from its staged copy, and each exit's section names the part of it to read first), and the licence to stop chats and apps.

- **The skill-exemption commit is not an edit of the worker's.** The worker commits what `validate_skill.py --stamp` wrote, alone, as `Record skill-check exemptions`. That commit selects no suite, and does not end the clean-pull skip of the review gates.

- **Fixed**: the catch-up's "all three" frontend bundles (there are seven), and a browser README path that does not exist.

`update-self` keeps an exemption for the `contents` check only, which it fails through two shared files. It links no file inside another skill: the review gates, the page-driving rules and the packaging migration guide it sends the worker to are shared references, named by their staged paths.
