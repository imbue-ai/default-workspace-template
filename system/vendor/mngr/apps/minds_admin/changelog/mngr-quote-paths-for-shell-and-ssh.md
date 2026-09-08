The pinned `known_hosts` path in the box ssh commands is now quoted for ssh.

ssh documents `UserKnownHostsFile` as a whitespace-separated list of files and splits the value itself, so a path containing a space became several nonexistent files and host-key verification failed. The path comes from `tempfile.mkstemp`, which honours `TMPDIR`, so it is not guaranteed to be space-free.
