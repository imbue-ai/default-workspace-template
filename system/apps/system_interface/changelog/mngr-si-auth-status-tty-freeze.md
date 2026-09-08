The shell now runs the one command it spawns -- the git read behind the update-staleness banner -- in its own session, so it has no terminal to reach and killing it cannot signal-stop the shell along with it. A ratchet keeps new code on that path.

This is the shell's share of a fix whose bulk is in the chat app, which is where the sign-in check that first triggered the freeze lives. The runner itself now lives in the shared `detached_subprocess` library, since both services are supervisord programs with the same exposure.
