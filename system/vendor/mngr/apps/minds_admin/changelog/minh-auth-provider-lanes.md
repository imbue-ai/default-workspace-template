Stop the pool bake waiting for an initial-chat sentinel that no longer appears.

DEFAULT_WORKSPACE_TEMPLATE no longer creates a chat at boot: a chat binds to a provider account
when it is CREATED and nothing rebinds it, so one made before anyone signed in could never take
a turn whatever the user later authenticated to. Finalize was waiting up to eight minutes for
that chat's sentinel on every bake, purely to reach its own "nothing to tear down" branch, and
then destroying an agent that does not exist.

The identity reset stays and is the reason this still works: the template's bootstrap sets an
only-if-unset git identity on every boot rather than behind a first-run signal, so what finalize
unsets here is always restored on adoption.
