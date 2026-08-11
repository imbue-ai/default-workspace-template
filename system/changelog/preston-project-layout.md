Update `system/scripts/layout.py`'s help for projects.

The workspace now groups tabs into projects rather than named layouts, and a connected browser client reports its active project as the thing an op can target. The `--layout` help and the module docstring now say so: mutating ops name the project a client is in (`context` reports it), and the older named layouts still resolve for compatibility even though nothing keeps one active.
