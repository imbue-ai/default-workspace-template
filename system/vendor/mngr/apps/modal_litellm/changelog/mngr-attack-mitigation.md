# Per-request access logging

- The LiteLLM proxy now logs one structured access-log line per request to the Modal function logs (method, path without the query string, status, duration, client IP from the first `x-forwarded-for` hop, user agent), via the shared `modal_app_kit` request-logging middleware. Part of the sign-up abuse/spam mitigation work.
