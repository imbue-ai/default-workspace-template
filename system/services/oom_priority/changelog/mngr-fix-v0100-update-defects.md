- The tagging table in the README lists a command an agent waits on through `run_in_background.py --waiter`. It runs in the user-service band (200) with its runner, and a shed one is started again while its agent runs.

- Each bare-tier script in `bin/` marks its `sys.path` edit with `# import-path: no-venv`, as the launch rules now require, saying why it runs without the venv.
