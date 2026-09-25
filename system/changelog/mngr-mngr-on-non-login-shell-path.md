- The image build symlinks `mngr` into `/usr/local/bin` like `tk`, so non-login shells
  (`ssh <workspace> mngr ...`, `mngr exec`) find it without a `PATH` prefix.

- `tool_env.tool_location` reports the bin directory that pairs with the tool directory
  its shebang names, rather than the directory the console script was found in, so a
  caller that reaches `mngr` through `/usr/local/bin` still installs entry points where
  the build writes them.
