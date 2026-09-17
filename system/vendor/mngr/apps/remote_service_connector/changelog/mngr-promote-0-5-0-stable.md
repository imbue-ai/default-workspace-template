The hardcoded installer link behind `/download` now points at 0.5.0, matching the stable channel.

It is only reached when the stable channel manifest cannot be fetched -- the endpoint reads the live manifest first -- so it is the answer given while the feed is unreadable, and must never name a build ahead of stable.
