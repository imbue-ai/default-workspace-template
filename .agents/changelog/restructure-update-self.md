`update-self` is restructured: SKILL.md is the lead's checklist, and each step's detail is in a reference the step names. No command of the update changed.

- **SKILL.md is 13.9 KB** (was 51.6 KB): standing instructions, the step list, then each step in a few lines. The launch, the question gates, the audit, the apply, the report-driven remainder and teardown each moved to a reference. The step headings are unchanged, so a lead arriving from an older release still enters at "Step 3".

- **`references/older-workspaces.md`** (new) holds everything true only for a workspace older than some release, each item with a `CLEANUP:` line. A step with such a difference points at its section in one line. `heal-shallow-history.md` is folded into it.

- **The worker guide is a core plus rare-path references.** The core tells the worker to re-read it after a compaction; conflict triage, the impact analysis, the validation items, the `done` report and the catch-up each have a file.

- **The impact analysis covers every name an update retires.** `update_self.py retired-names --base <ref> --target <ref>` (new) lists removed `[project.scripts]` commands and removed or renamed skill directories as JSON. The worker also reads the template's and mngr's changelog entries for removed flags, environment variables, ports and routes, and searches the user's creations for each name. A use with a stated replacement is rewritten in its branch; any other hit is reported.

- **One home each** for the ceiling's rules, the apply's exit codes, and the licence to stop chats and apps.

- **The skill-exemption commit is not an edit of the worker's.** The worker commits what `validate_skill.py --stamp` wrote, alone, as `Record skill-check exemptions`. That commit selects no suite, and does not end the clean-pull skip of the review gates.

- **Fixed**: the catch-up's "all three" frontend bundles (there are seven), and a browser README path that does not exist.

`update-self` keeps an exemption for the `contents` check only, which it fails through two shared files.
