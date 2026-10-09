The project requires Python 3.12 or newer (was 3.11), matching the workspace floor.

`select-tests` no longer names `system/scripts/stdlib_only_scripts_test.py` among the always-run guards; the root `system/test_no_type_errors.py`, which runs with every `system/*.py`, replaces it.
