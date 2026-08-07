Fixed opencode's TUI failing to start with "Failed to initialize OpenTUI render library:
failed to map segment from shared object". opencode is a Bun binary that extracts its
OpenTUI native render library to the OS temp dir and maps it executable; with `TMPDIR`
unset it used `/tmp`, which is mounted `noexec` in the workspace image, so the mapping was
rejected. The opencode launch env now sets `TMPDIR` to a per-agent, exec-capable dir under
the agent state dir (`<state>/plugin/opencode/tmp`, created by the launch script), joining
the existing per-agent config/data isolation. Applies to both the server and the attach
(TUI) process.
