Release Tests now actually exclude the `apps/minds` tree, as the workflow always intended.

Both jobs passed `--ignore apps/minds`, which has no effect here. The root `testpaths` expands `apps/*` into one path per project, so `apps/minds` reaches pytest as an *initial path*, and pytest does not run `pytest_ignore_collect` against initial paths -- which is where both `--ignore` and `--ignore-glob` are implemented. Anything naming that directory is therefore a no-op. Minds release tests were collected and run by the mngr release jobs as well as by their own jobs in `ci.yml`; observed before this change, that was the sole cause of the Docker job's failure -- 24 passed, 2 skipped, and the only 4 errors were minds tests that should never have been collected.

The flag is now `--ignore-glob='apps/minds/*'`. The trailing `/*` is what makes it work: it matches the files *inside* the tree, which are not initial paths, rather than the directory itself.

The comment above these jobs said they cover the mngr release suite only, which was never true: release tests under other `apps/` projects are collected here too. It now just states what the jobs run, without naming the flag it sits above.
