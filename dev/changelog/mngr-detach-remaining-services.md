Every supervisord program in the workspace now spawns its subprocesses detached from the workspace's terminal, and each carries a ratchet that keeps it that way.
