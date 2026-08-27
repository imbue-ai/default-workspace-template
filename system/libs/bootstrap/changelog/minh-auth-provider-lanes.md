Do not create the workspace's initial chat before anyone has signed in to a provider.

A chat binds to a provider account when it is CREATED -- the credential rides `mngr create`'s
own flags -- and nothing rebinds an existing agent. So a chat made at boot, before any account
exists, can never take a turn no matter what the user signs into afterwards, and the path that
used to rescue it (write the shared credential, restart every claude agent) is gone.

The workspace now boots to the new-tab screen with no agents, where the provider chooser is the
way forward. `_initialize_workspace_main_branch` still runs, and the signal file is still
written, so the decision is made once per workspace and `pool_bake` stops waiting on it.
