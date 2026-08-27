Do not create the workspace's initial chat before anyone has signed in to a provider.

A chat binds to a provider account when it is CREATED -- the credential rides `mngr create`'s
own flags -- and nothing rebinds an existing agent. So a chat made at boot, before any account
exists, can never take a turn no matter what the user signs into afterwards, and the path that
used to rescue it (write the shared credential, restart every claude agent) is gone.

The workspace now boots to the new-tab screen with no agents, where the provider chooser is the
way forward. `_initialize_workspace_main_branch` still runs, and the signal file is still
written, so the decision is made once per workspace and `pool_bake` stops waiting on it.

Separate the workspace's three first-boot steps, which shared one signal file.

The committer identity now runs on EVERY boot rather than once. It is the workspace's only one
-- nothing else in the repo sets `user.email` -- and `pool_bake` deliberately unsets it on
finalize, expecting the adopted workspace's bootstrap to supply it again. Behind a one-shot
signal, an adopted workspace could not commit at all: the user's own terminal, github-sync and
any script would fail, and only agent tool calls survived, via the env vars the bash wrapper
exports.

Putting the work_dir on `main` gets its own one-shot signal and moves to `main()`. It is a
once-ever operation -- `git add -A` on a later boot would sweep up whatever the user had in
flight -- but "does this workspace have a main branch" and "does it have a chat" are different
questions and no longer share an answer.
