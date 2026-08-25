Fixed a freeze that made a workspace go blank: the system interface would stop answering entirely, its window showing white, health probes failing, and the app eventually reporting "Lost connection to this machine". The process was not crashed or hung on work -- it was signal-stopped.

The cause was the sign-in check. On every page load the system interface runs `claude auth status`, and it kills that command if it overruns its budget. The `claude` CLI opens the terminal device directly, even with its output redirected, and it reads that terminal and restores its modes while shutting down. Because supervisord leaves every service it starts in a background process group on the workspace's terminal, both of those made the kernel stop the whole group -- the system interface along with the command it was killing. The socket kept accepting connections and nothing ever answered them.

Now every subprocess the system interface starts runs in its own session, with no terminal to reach, so killing one can no longer take the service down with it. A ratchet keeps new code on that path.

The sign-in check also got a more realistic budget. It reads a local credentials file, so a warm run takes a fraction of a second, but the first run after the workspace has been idle spends seconds paging the 256MB `claude` binary back in -- measured at 3-8s in a real workspace, and over the old 10s limit three times in sixty checks. The budget is now 25s.

Finally, a check that does run out of time no longer reports "signed out". It reports that it could not tell, and the status endpoint answers 503 instead of guessing, so a slow check can no longer pop the sign-in dialog over a workspace that is signed in.
