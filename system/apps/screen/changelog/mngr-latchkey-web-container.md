- Gate mode, for the web latchkey auth sandbox, which serves the Screen over Modal's TLS with no share gateway in front of it: when `SCREEN_OWNER_TOKEN_SHA256_FILE` is set, a loopback request is still the owner, the file it names is read on every other request for the owner token's SHA-256 (while it is absent, empty or malformed only loopback is admitted, and a malformed one is logged, so a pooled sandbox's Screen can start before anyone claims it), a GET with a matching `?owner_token=` sets the owner session (`screen_owner_session` and the partitioned `screen_owner_session_partitioned`, host-only, an HMAC under a secret held in memory) and redirects to the same URL without the token, and every other request, WebSockets and the `/neko/` proxy included, answers 401 without that session. Every cookie value under either name is tried, so a same-named cookie another `modal.host` page set cannot mask the session. In gate mode `X-Imbue-Identity` is ignored and every response carries `Content-Security-Policy: frame-ancestors <SCREEN_FRAME_ANCESTORS>`. The access log shows every `owner_token` value as `REDACTED`. Without the variable the service behaves as before.

- `screen-service --host` (or `SCREEN_LISTEN_HOST`) binds an address other than `127.0.0.1`; the service refuses one that is not loopback unless gate mode is on.

- `GET /api/connect-info` also reads the connect-info document in the file `SCREEN_CONNECT_INFO_PATH` names, on every request, when that variable is set; an absent or unreadable file is a broker that did not answer.

- `GET /api/connect-info` answers a candidate a broker named by a hostname as one candidate per IPv4 address the name resolves to, since Firefox ignores a remote ICE candidate whose address is a name; a name that does not resolve drops that candidate, with a log line, never the answer.

- `SCREEN_VIDEO_CODEC` (`h264`, the default, or `vp8`) picks the codec neko streams in. With `vp8` the three profiles keep their frame rates and bitrates as libvpx realtime CBR pipelines, which Firefox decodes without Cisco's OpenH264 plugin.

- `SCREEN_FIXED_SIZE=WxH` (a size a viewer's report could produce) keeps the display at one mode: the X server and neko start at it, and `POST /api/screen-size` answers `{"applied": false, "reason": "fixed size"}` without resizing, as `screen size WxH` then says, exiting 1. A value the service cannot use stops it at start.

- The Screen requires Werkzeug 3.1 or later, whose `set_cookie` takes the `partitioned` flag the gate sets.
