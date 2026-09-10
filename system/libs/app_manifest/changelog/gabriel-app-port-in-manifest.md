An app manifest can now declare `url`, the loopback origin the app serves its own
pages at. That is what lets tooling which never starts an app learn the port it
holds, and for an app that registers itself at runtime rather than through its
supervisord command it is the only record of that port committed to the repo.

It is validated by the same rule as `instances_url` (now shared as
`describe_loopback_url_problem`), and a manifest that points both at the same
port is rejected -- one socket cannot serve both. `loopback_url_port` reads the
port out of either.
