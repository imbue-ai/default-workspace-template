Review integration of the projects follow-up work (PR #451, branch preston/projects-followup-ideas), merged onto main for verification; see system/apps/system_interface/changelog/preston-projects-followup-ideas.md for the full description of that work.

On top of the merge, one defect found during review is fixed: saving a project's settings (rename, color, or glyph) no longer silently resets the built-in shortcut rows the project had unpinned back into its rail -- update_project now carries the unpinned_shortcuts field through the rebuilt registry entry, with a regression test.
