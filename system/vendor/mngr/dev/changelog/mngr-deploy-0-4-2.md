The CI `cleanup-vultr-instances` job now retries transient Vultr API
failures (HTTP 5xx and network-level request failures) with exponential
backoff instead of failing the whole pipeline on a single provider-side
500. Deterministic client errors (4xx) still fail immediately, and the
retried cleanup pass is idempotent.
