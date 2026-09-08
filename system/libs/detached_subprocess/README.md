# detached_subprocess

The one way a workspace service shells out.

A service started by supervisord keeps the session's controlling terminal -- in a workspace,
the tmux pane running `uv run bootstrap` -- and every child it spawns lands in a *background*
process group on that terminal. A child that then reads the terminal or changes its modes (the
`claude` CLI does both, even with stdio redirected) is answered by the kernel with SIGTTIN /
SIGTTOU addressed to the whole group, which stops the service mid-call: its socket keeps
accepting connections and nothing ever answers them.

`run_detached_command` puts each child in its own session, so it inherits no controlling
terminal and there is no group for a terminal signal to travel through.

- `runner.py` -- `run_detached_command`, the entry point itself.
- `ratchets.py` -- the two rules that keep an app's spawns going through it. Each consuming app
  applies them to its own source tree from its own `test_subprocess_ratchets.py`.

See `runner.py`'s module docstring for the mechanism in full, and for the cost of detaching: a
detached child is out of reach of supervisord's process-group signals, so its own timeout is
what bounds it.
