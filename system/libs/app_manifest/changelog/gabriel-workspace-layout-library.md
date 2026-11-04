`app_manifest.shell_windows` (the shell URL lookup and the window readers) moved to the new `workspace_layout` library, along with the `ShellStub` test stand-in, which `workspace_layout.testing.LoopbackShell` replaces.


`registry_path()` and the registry location constants (`DEFAULT_APPS_FILE`, `ENV_APPS_FILE`) moved to the new dependency-free `app_manifest.registry_location`, so a caller can find the registry without importing pydantic.
