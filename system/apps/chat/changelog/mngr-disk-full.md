The chat app keeps running when the workspace's disk is full.

- A registration the full disk refuses no longer ends the app at start (it used to restart in a loop until space was freed); it serves at once and writes its row once there is room.

- A request the full disk refuses answers 507 with "The workspace's disk is full. Delete files you no longer need, then try again." instead of a 500 carrying the OS error. An attachment the full disk refuses says the same in the composer, and leaves no partial copy behind.
