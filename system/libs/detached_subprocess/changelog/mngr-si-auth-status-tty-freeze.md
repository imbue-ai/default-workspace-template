New library holding the one way a workspace service shells out. `run_detached_command` runs
each child in its own session, so it inherits no controlling terminal and cannot stop its
parent service by reading that terminal or restoring its modes when it is killed -- the failure
that left the workspace's socket accepting connections nobody answered.

The library also carries the two ratchet rules that keep spawns going through it, which the
chat app and the system interface each apply to their own source tree. It lives here rather
than in either app because both are supervisord services that spawn, and the chat app cannot
depend on the system interface at runtime.
