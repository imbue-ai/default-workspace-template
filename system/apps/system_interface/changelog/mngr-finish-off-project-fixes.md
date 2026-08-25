Review integration of the projects follow-up work (PR #451, branch preston/projects-followup-ideas), merged onto main for verification; see system/apps/system_interface/changelog/preston-projects-followup-ideas.md for the full description of that work.

On top of the merge, one defect found during review is fixed: saving a project's settings (rename, color, or glyph) no longer silently resets the built-in shortcut rows the project had unpinned back into its rail -- update_project now carries the unpinned_shortcuts field through the rebuilt registry entry, with a regression test.

The File Viewer comes to life: the rail row, launcher tile, and All apps entry now open the workspace's new "files" app (dufs serving data/) wherever it is registered, staying disabled with the old coming-soon tooltip on workspaces built before the service shipped.

Legacy url: members are purged from the project registry on first read: ad-hoc pages are no longer filed as members, and the dead entries the old filer wrote (naming a panel, not a page) would otherwise linger until removed by hand. Pages themselves persist as panels in each view's saved arrangement.

The shortcuts endpoint answers a soft no-op instead of a 500 when no primary agent is configured (dev/test setups), matching the add-member endpoint beside it; body validation still runs first.

Two exported ref parsers nothing called (terminalSessionFromRef, browserSessionFromRef) are deleted, and the tests that spawn a real mngr observe now isolate MNGR_HOST_DIR so they no longer enumerate the developer's own agents (which tripped the tmux resource guard on machines with live agents).
