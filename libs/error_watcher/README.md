# error_watcher

Background service that scans every tmux window in its session for output
matching `/error|exception/i` and, on newly-appeared matches, sends one message
to a randomly selected mngr agent.

Fuller documentation lands alongside the service registration.
