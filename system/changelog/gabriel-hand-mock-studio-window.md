- The lock records `agents-shared`'s new dependency on `workspace-layout`, which `serve_isolated_instance.py down` uses to find and close the windows still showing an instance.

- The critical-app test plan's teardown steps use `preview_app.py down --close-windows`, as the careful-flow reference now does, and B9 checks that `serve_isolated_instance.py down` refuses under the preview's open tab until given `--close-windows`.

- `workspace_layout.testing.LoopbackShell` takes `post_answers`, what each POST route besides the op and client-activity routes answers, so a test can stand in for the shell's window close route.
