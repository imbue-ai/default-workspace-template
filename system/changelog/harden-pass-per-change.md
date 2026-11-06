`docs/system/workspace-internals.md` describes a harden pass as covering one change: every creation it touched, with one worker, each loaded with its own type reference.

The workspace app model's contract for the update notice (`docs/system/blueprint/workspace-app-model/contracts.md`, section 5) says its `apps` include every other app of the harden pass the apply landed, not only the critical ones.
