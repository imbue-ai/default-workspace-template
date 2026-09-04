# Submitting mngr changes

mngr runs in this workspace as Python packages installed from the public mngr repo
at the commit `pyproject.toml` pins (`[tool.uv.sources]`, `imbue-mngr`). There is
no git checkout of it here. In a released workspace the installed files under the
tool's `site-packages` are a build of that commit, and the next reinstall (any
`mngr plugin add`, the update-self refresh, `uv sync`) puts that build back. In a
workspace built against an mngr checkout (`system/vendor/mngr` exists; how
`just minds-start` in the mngr repo builds them), that tree is an editable install,
so an edit there runs -- but it is a copy with no `.git`, so it cannot be
submitted either.

**The rule: mngr changes are not template changes. They get their own PR on the
mngr repo, developed in a standalone checkout.** Once that PR merges and reaches
the public mirror, the template picks it up by moving the pin -- the template PR
usually needs to carry nothing but that pin bump, if anything.

## Flow

1. Create a standalone mngr checkout:

   ```bash
   git clone git@github.com:imbue-ai/mngr-internal.git .external_worktrees/mngr
   git -C .external_worktrees/mngr checkout -b <branch-name> origin/main
   ```

   Name the branch after the current workspace branch when that makes sense,
   and NEVER leave the checkout sitting on `main` -- mngr's committed
   code-guardian policy applies to it as a normal mngr clone, including
   merge-and-push on stop. The path must be exactly `.external_worktrees/mngr`:
   that is the directory `.reviewer/settings.json` lists under
   `stop_hook.additional_git_directories`, so the stop hook reviews work there
   alongside the workspace once it exists.

2. Make the change there, and test it there with mngr's own test suite (its
   CLAUDE.md governs how). To exercise it inside this running workspace, point
   the workspace's mngr at the checkout for the duration of the test and put the
   pin back afterwards:

   ```bash
   # try the checkout's mngr as the workspace's tool (leaves pyproject.toml alone)
   uv tool install -e .external_worktrees/mngr/libs/mngr \
       --with-editable .external_worktrees/mngr/libs/mngr_claude   # plus the other plugins you need
   # ...test...
   # restore the build pyproject.toml calls for
   bash system/scripts/install_mngr_tools.sh
   ```

3. Commit in the checkout and follow mngr's own conventions from there (its
   CLAUDE.md governs; unlike this repo, mngr expects a draft PR on the mngr
   repo, and the code-guardian gates on the checkout will hold the stop until
   the branch is pushed and reviewed).

4. After it merges: bump the pin here by editing the `rev` under
   `[tool.uv.sources]` in `pyproject.toml` to the public-mirror commit that
   carries it, then `uv lock` and `bash system/scripts/install_mngr_tools.sh`.
