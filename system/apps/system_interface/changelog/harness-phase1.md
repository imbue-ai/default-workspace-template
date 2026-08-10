- The destroy subprocess cap is a named constant raised from 30s to 120s (a real destroy measured ~16s idle and degrades under load; the old cap SIGTERMed destroys mid-teardown and surfaced them as 500s).

- HTTP errors (404/405) keep their real status codes instead of being wrapped into 500s by the unhandled-exception handler.

- The shared model matcher also accepts catalog option ids (e.g. claude's "opus[1m]"), so a provision-time seeded model state matches before the harness has reported anything.
