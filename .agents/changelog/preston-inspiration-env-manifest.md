Inspirations now declare what they need from the environment, and a published
repo holds exactly one of them.

An inspiration publishes `inspiration.md`, `inspiration.toml`, and
`inspiration.svg` -- no slug in any filename. Publishing or adopting one
OVERRIDES the previous manifest instead of piling up beside it; what survives
is a lineage chain recording each predecessor's repo URL and the exact commit
it was used at, so a superseded manifest stays readable in the repo where it is
authoritative.

The new `inspiration.toml` is the machine-readable half: the derivation recipe
(moved out of the markdown, where it was the last YAML in the repo), the
structured prerequisites, the inspirations this one was built on, and a new
`[environment]` section declaring the apt packages, npm globals, uv tools,
cargo crates, and `env.d` units the code needs. Adopting an inspiration
converges those at the ADOPTING mind's own pinned apt snapshot timestamp, so
versions come out consistent with the rest of that environment; a package that
cannot be installed says so, and says whether an upgrade would fix it.

Publishing now validates the manifest against a pydantic schema and checks that
every declared apt package resolves in the pinned mirror -- so an unmirrorable
dependency is caught when it is published rather than when someone adopts it.

The manifest's "Holes" section is now called "Requirements", and the generated
README is a real landing page: a hero graphic, an "Open in Minds" button, why
you care, how to use it, and ideas for making it yours.

Inspirations published in the older format keep working when adopted; the
publisher's next update migrates them.
