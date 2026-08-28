Added `specs/user-suspension.md`: the plan for reversible user-account suspension across the minds/imbue-cloud stack (operator session revocation, immediate revocation on state-modifying routes, and the suspend/unsuspend fan-out), covering issue #550.

Added a repo-wide meta ratchet (`test_numbered_sql_migrations_have_unique_numbers`) asserting that no `migrations/` directory holds two `NNN_*.sql` files with the same number -- the failure mode two concurrent branches produce (it happened with the connector's 029).
