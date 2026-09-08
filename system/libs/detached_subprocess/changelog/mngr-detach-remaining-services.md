Two more ways to shell out safely. `run_detached_subprocess` is the plain-`subprocess.run` shape, for a service that is not built on ConcurrencyGroup and wants to keep `CompletedProcess` and the stdlib's exceptions. `spawn_detached_process` starts a child that outlives the call -- a wrapped server, a display, a browser -- and hands back its handle.

The caller owns a long-lived child's death: detaching puts it out of reach of supervisord's group kill, so the handle is now the only thing that stops it.
