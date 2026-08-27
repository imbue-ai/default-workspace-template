The download fallback now names the build stable serves, rather than ToDesktop's
own channel URL. That URL points at whatever was last Released in ToDesktop,
which has not tracked our stable channel since release channels landed -- so an
unreadable feed could hand someone a build from a different channel entirely.

A failed read of the channel manifest is also retried once before giving up. The
retry is bounded at two attempts on purpose: `GET /download` is a sync route, so
each attempt holds one of the worker threads the rest of the connector shares.

A link the connector cannot resolve is now logged at error rather than warning
level, carrying whatever actually failed, so it reaches Bugsink as an
error-level event with a stack rather than a warning among the routine noise.
The feed is expected to be readable, and while it is not, every download
serves the fallback. Each way a manifest can be unusable says which one it was,
so a feed serving broken bytes and a feed serving the wrong shape land as
separate issues rather than one.

A manifest the parser cannot turn into a document now falls back like any other
unusable one, whether or not it failed to *parse*. Deep nesting exhausts the
stack instead, and a scalar that resolves but will not convert -- an
out-of-range date, an integer past the digit cap, `!!bool` on a non-bool --
raises out of the constructors unwrapped. Those used to escape and answer the
download link with a 500, and to cache nothing, so every download re-read the
feed and raised again.
