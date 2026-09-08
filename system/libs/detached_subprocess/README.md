# detached_subprocess

The one way a workspace service shells out.

A service started by supervisord keeps the session's controlling terminal -- in a workspace,
the tmux pane running `uv run bootstrap` -- and every child it spawns lands in a *background*
process group on that terminal. A child that then reads the terminal or changes its modes (the
`claude` CLI does both, even with stdio redirected) is answered by the kernel with SIGTTIN /
SIGTTOU addressed to the whole group, which stops the service mid-call: its socket keeps
accepting connections and nothing ever answers them.

Every entry point here puts its child in its own session, so the child inherits no controlling
terminal and there is no group for a terminal signal to travel through.

- `runner.py` -- the three entry points. `run_detached_command` runs a command to completion in a
  service built on `ConcurrencyGroup` (the chat app, the system interface);
  `run_detached_subprocess` does the same for a service that is not, keeping
  `subprocess.CompletedProcess` and the stdlib's exceptions; `spawn_detached_process` starts a
  child that outlives the call and hands back its handle.
- `ratchets.py` -- the two rules that keep a service's spawns going through those. Each consuming
  project applies them to its own source tree from its own `subprocess_ratchets_test.py` -- named
  `test_subprocess_ratchets.py` in the chat app and the system interface, which are exempt from
  `system/test_meta_ratchets.py` and follow the mngr monorepo's naming.

Only `run_detached_command` needs mngr; the other two are a few lines of stdlib. What the library
carries is less the code than the name and the rules: one place to point a ratchet at, and one
place that explains why.

See `runner.py`'s module docstring for the mechanism in full, and for the cost of detaching: a
detached child is out of reach of supervisord's process-group signals, so its own timeout is
what bounds it.

A long-lived child is the caller's to kill. Detaching removes it from the service's process
group, so supervisord's `stopasgroup`/`killasgroup` no longer reaps it. Terminate it through the
handle, and if it holds a fixed port, sweep for an orphan at startup -- a service SIGKILLed for
overrunning `stopwaitsecs` never runs its own teardown, and the orphan then holds the port
against its own restart.
