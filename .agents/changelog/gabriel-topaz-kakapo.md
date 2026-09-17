An update no longer installs a `mngr` that nothing on PATH can run.

When the apply could resolve neither the tool it was refreshing nor `mngr` to a uv tool on PATH, it let uv pick the tool directory. uv's default follows `$HOME`, which in a workspace is `/home/user` -- a directory the image's PATH, the login profile and supervisord all leave out. The install then exited 0 and the apply reported UPDATED, so a workspace whose `mngr` had been deleted, or an old lease that never had a uv-tool `mngr` to resolve in the first place, could update itself into having no working `mngr` at all.

The apply now falls back to the same pinned tool home the build installs under, taken from the `tool_env` module both trees already share, so the floor cannot drift from the build's target. Reading a tool's recorded plugins follows the same directory rather than asking `uv tool dir`, which would answer for the `$HOME` being overridden.

The apply's tests now run with that pinned home redirected at a temporary directory, so a test that resolves nothing from its fake PATH can no longer reach the real `/root` of whatever machine runs the suite.
