The footprint command's diff can now run to a ref other than HEAD, and reports what the footprint accounts for as well as what it does not.

Before, `app-manifest footprint --diff-base` always diffed up to HEAD and listed only `outside_footprint`, so a caller standing on a merge commit could not read either side of it, and had to work out for itself which changed files were the creation's own.

Now `footprint` takes `--diff-ref` beside `--diff-base`, and the scope file's `diff` gains `ref` (the full sha the diff runs to) and `inside_footprint` (the changed files the footprint accounts for; excluded files are in neither list). One tree can answer for a range that does not end at HEAD, which is what the update-self worker needs to read a merge commit's two sides over the same footprint.
