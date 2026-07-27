Integration branch combining the user-data-layout trains (`mngr/fix-data-layout`, `mngr/declutter-template`) with `mngr/fix-apt-mirror`; the full per-train details live in this project's sibling entries for those branches.

For this project: the standalone snapshot-pinned apt mirror (`https://apt.imbuepackages.com`) arrives via the merged `mngr/fix-apt-mirror` train -- a Cloudflare Worker serving frozen `dists/` sets and a read-through pool cache from R2, plus the `apt-mirror` operator CLI (`cut|warm|verify`) authenticated directly with R2 credentials.
