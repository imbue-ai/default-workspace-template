`minds-admin env activate` now writes `update_feed_base_url` into a dev env's
`client.toml` when the env config sets one, and omits the key when it does not.

That key is what tells the desktop app where to read release-channel manifests
from. An env without it offers the stable channel only, reading ToDesktop's own
feed, which is the correct behaviour for an env that publishes no manifests --
so the key is written only when there is something to point at.
