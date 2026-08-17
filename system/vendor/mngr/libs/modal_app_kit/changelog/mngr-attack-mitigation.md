# Request-logging middleware

- New `imbue.modal_app_kit.request_logging.RequestLoggingMiddleware`: a pure-ASGI middleware that emits one structured access-log line per HTTP request (method, path without the query string, status, duration, client IP from the first `x-forwarded-for` hop, user agent) so Modal function logs carry a per-request record for abuse investigations. The client-controlled fields (path, user agent, forwarded client IP) are quoted/sanitized so a crafted request cannot forge fields or lines in the log. Wired into the remote service connector and the LiteLLM proxy.
