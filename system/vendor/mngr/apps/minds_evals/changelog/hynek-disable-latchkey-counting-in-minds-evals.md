Eval boxes no longer count toward Latchkey's usage. Every trial boots the real Minds desktop stack,
which spawns `mngr latchkey forward` and a `latchkey gateway` with it, and that gateway emitted the
per-host daily ping that registers a Latchkey user -- so a nightly run registered one user per
trial. The per-trial box env now carries `LATCHKEY_DISABLE_COUNTING=1`, which the gateway inherits.
The workspaces a box creates were already covered: mngr injects the same var into every agent it
wires a gateway for.
