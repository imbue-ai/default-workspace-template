`app-manifest select-tests` selects from declared structure only.

- A changed path inside a package or skill runs that unit's suite, plus the suites of the workspace members and npm packages that depend on it and of the apps whose manifests reference it. A supervisord block runs the app whose program it holds, and `uv.lock` runs the members that depend on what it upgraded.

- Any other path (a flat script, an agent hook, repo-level config) runs the full root suite.

- The override file, the scans for test files that name or import a changed path, flat-script filename pairing, and the test requiring every tracked path to map are gone.

- The always-run set gains `test_skill_mngr_references.py` and `dispatch_contract_test.py`, which read every skill's prose, so a change to any one skill's prose runs them.
