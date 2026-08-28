# deflake-mngr-ci: test-integrity and happens-before rules for flake fixers

Tightened the `deflake-mngr-ci` skill with two non-negotiable rules for agents fixing CI flakes, so a "deflake" can never quietly degrade the suite's ability to catch bugs:

- **Never weaken a test to buy reliability.** A test's value is its power to fail on broken code; loosening assertions, widening tolerances, sleeping or retrying around an assertion, or skipping / xfail-ing manufactures false negatives. Gates must fail closed, never open. (Marking an open flake `@pytest.mark.flaky` is still valid triage while its ticket is open -- it retries a known flake to keep CI usable, and is never itself the fix.)

- **Never "settle-then-assert."** Polling for quiescence, arbitrary sleeps, and retry-until-green paper over a missing happens-before edge instead of establishing it. The correct fix establishes it explicitly -- await a specific completion signal or observable condition, impose a barrier or causal ordering, inject a controllable clock, or remove the shared-mutable-state race at its source -- so the outcome is deterministic rather than probabilistically settled. Wall-clock waiting is legitimate only when elapsed real time is genuinely part of the specification under test.
