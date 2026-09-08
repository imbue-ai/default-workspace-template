Environment convergence now runs apt, npm and the env.d unit scripts in their own sessions, so a package manager drawing progress on the workspace's terminal can no longer stop the service that started it.

`apt-get update`'s output is now logged explicitly rather than inherited, because a detached child's output is captured rather than passed through.
