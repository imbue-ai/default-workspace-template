`GET /download?platform=mac-arm64` now serves the build the **stable release channel**
serves, rather than ToDesktop's own channel URL.

Those were one pointer until release channels landed: clicking Release in
ToDesktop moved both. Stable now moves by merging a change to
`apps/minds/release-channels.toml`, and ToDesktop's own pointer does not follow
it -- so the public download link could hand someone a build stable was not
serving, and nothing would have said so.

The target is read from the channel manifest and cached for a minute, not
written in at release time: the connector deploys on its own schedule, so a
baked-in value would not reach the running service until somebody redeployed it.
A manifest that cannot be read leaves ToDesktop's channel URL in place, matching
the fail-open behaviour of the attribution write in the same request -- a
download that works beats one that does not.

That failure is cached for the same minute, so an outage costs one download the
fetch timeout rather than all of them. The fetch gives up after two seconds:
`/download` is a sync route, so it holds one of the connector's shared worker
threads for however long the feed takes to answer.

Only a url on ToDesktop's download host is a candidate. The feed decides where
the link sends people, so a manifest naming somewhere else -- or naming a bare
filename, which a browser would resolve against the connector's own host --
falls back instead of being served.
