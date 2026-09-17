Periodically run context compaction on active chat agents in the background.

- **Background compaction sweeps:** Adds `ChatAutoCompactor`, which periodically sweeps active chat agents and runs `mngr autocompact run <agent_name>` to compact context before limits are exceeded.
- **Parallel agent checking:** Sweeps running agents concurrently via a worker pool, ensuring fast passes across multiple active agents.
- **Lifecycle integration:** Integrates the compactor into `ChatAgentManager` so background checks start automatically on launch and terminate cleanly on shutdown.
