The shell keeps running when the workspace's disk is full.

- It registers its own row from its entry point instead of running `forward_port.py ... &&` before it, so a registration the full disk refuses no longer stops the shell from starting (it used to restart in a loop, leaving the workspace with no interface until space was freed). The row is written once there is room.

- A request the full disk refuses (creating a desktop, moving windows, any state write) answers 507 with "The workspace's disk is full. Delete files you no longer need, then try again." instead of a 500 carrying the OS error.
