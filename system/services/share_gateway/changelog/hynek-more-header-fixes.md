Shared workspaces now answer every request with the browser hardening headers a security scan expects, whatever the service behind the origin sends:

- caddy stamps `Strict-Transport-Security: max-age=31536000; includeSubDomains` and `X-Content-Type-Options: nosniff` on every response of the share and strips the `Server` header, so a service's own (the Flask development server's `Werkzeug/x Python/y`, for one) no longer reaches a visitor. The operations are deferred, so they apply after the service's response headers land, and the auto-retrying loading page caddy serves for an unreachable backend carries them too. One visible consequence of `nosniff`: a script or stylesheet a service serves under the wrong MIME type is no longer sniffed into working by the browser.

- The gateway's own pages (loading, "Not shared with you", "Sign-in link expired", "Sharing is misconfigured") carry a full `Content-Security-Policy` admitting only their inline stylesheet by hash, beside the `frame-ancestors` policy caddy adds to everything.

A service's own `Content-Security-Policy` is still left in place and composed with the gateway's `frame-ancestors` policy; the gateway sets no page policy for services, since only a service knows what its pages load.
