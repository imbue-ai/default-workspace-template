`write_workspace_profile_snapshot` writes the profile snapshot a client sent, for the shell's profile route: it refuses a document that is not a snapshot's shape and keeps one of a newer format.

New module `timezone`: `read_user_timezone` and `write_user_timezone` read and record the user's IANA timezone (`data/.state/user_timezone`), by default only when none is recorded.
