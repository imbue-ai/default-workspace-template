- CI's pytest runs, on the runner and in the three offload configs, now carry a fake agent identity (`MNGR_AGENT_ID`, `MNGR_AGENT_NAME`, `MINDS_CHAT_ID`), as an agent's test run has one. The pytest-workspace-env plugin clears it before any test code runs, so it only shows when a pytest root fails to load the plugin, and then that root fails in CI instead of in an agent.

- The script tests' own fixture that cleared `MINDS_CHAT_ID` and `MNGR_AGENT_ID` is gone: the plugin now clears the whole agent environment for every test run.

- The root pytest configuration requires the pytest-workspace-env plugin (`required_plugins`), so a root run that cannot load it stops before collecting anything.
