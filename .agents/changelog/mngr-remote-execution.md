Add the `run-on-users-computer` skill: when a task would run a command on the
user's own computer, it says what to try first (file sharing and synced
folders, connected services, computing the result here), that a one-off script
the user runs themselves is fine, and that Imbue Studio's remote-execution
route is the only sanctioned way for an agent to run something there itself,
behind the user's own switch and a per-workspace approval. It tells the agent
to ask for the narrowest program that does the job (a bare name, or a path it
already knows such as a script it wrote under `~/.studio/custom-scripts/`)
rather than for any command, and that a shell or language runtime counts as
any command. It forbids leaving any standing way in (a daemon, a cron entry, a
listener, a tunnel, a polling script). AGENTS.md and the `file-sharing` skill
point at it, and the chat's permission card titles a remote-execution request
"This computer".
