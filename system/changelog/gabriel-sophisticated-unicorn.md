The share gateway's werkzeug floor moves from 3.0 to 3.1, and the root lock is
regenerated to match. 3.1 is the first release whose `Response.set_cookie`
takes `partitioned`, which the gateway now relies on to emit the CHIPS
`Partitioned` attribute instead of rewriting the rendered `Set-Cookie` header
itself.
