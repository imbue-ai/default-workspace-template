Fixed the root cause of the flaky `sentry_test.py` tests (MIND-228) and dropped
their `@pytest.mark.flaky` markers.

The sentry-sdk `LoggingIntegration` is process-global: every WARNING-or-worse log
record anywhere in the process becomes an event on whichever client is active, so
each test's in-memory capturing transport was a shared sink that unrelated,
concurrent warnings (a sibling test's logger, a background thread) also landed in.
Asserting an exact event total (`len(events) == 1`) therefore hard-failed whenever
a stray warning slipped into the window. The tests now select captured events by
their own unique probe instead of counting the sink's total, so unrelated events
can no longer inflate the count; a new regression test pins this by emitting an
unrelated warning alongside the intended one. The earlier "sandbox load" / DNS
timeout diagnoses were incorrect -- the import-cost timeout was already handled by
the session-scoped integration warm-up fixture.
