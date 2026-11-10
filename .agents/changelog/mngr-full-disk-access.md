`read-app-data` says that "the apps I have installed" and "my recent projects" mean the user's Mac, never the workspace's own apps or folders, and an agent with no grant asks for one rather than describing the workspace.

`read-app-data` tells the gateway's "Request not permitted by the user" (the grant is gone) from a 403 the Mac itself returns (Full Disk Access), withdraws a request still waiting once its source is `RESTORED`, and says "you approved" only after a `GRANTED` verdict.
