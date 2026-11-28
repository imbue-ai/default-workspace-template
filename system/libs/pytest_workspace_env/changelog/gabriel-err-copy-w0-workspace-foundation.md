The type-check ratchet is marked flaky, so offload retries it: its `ty` subprocess has stalled past the 10 s test timeout in CI sandboxes (MIND-684), while the check itself passes.
