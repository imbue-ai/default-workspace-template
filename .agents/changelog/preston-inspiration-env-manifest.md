Templates now declare what they need from the environment, and a published
repo holds exactly one of them.

Atemplate publishes `template.md`, `template.toml`, and
`template.svg` -- no slug in any filename. Publishing or adopting one
OVERRIDES the previous manifest instead of piling up beside it; what survives
is a lineage chain recording each predecessor's repo URL and the exact commit
it was used at, so a superseded manifest stays readable in the repo where it is
authoritative.

The new `template.toml` is the machine-readable half: the derivation recipe
(moved out of the markdown, where it was the last YAML in the repo), the
structured prerequisites, the templates this one was built on, and a new
`[environment]` section declaring the apt packages, npm globals, uv tools,
cargo crates, and `env.d` units the code needs. Adopting atemplate
converges those at the ADOPTING mind's own pinned apt snapshot timestamp, so
versions come out consistent with the rest of that environment; a package that
cannot be installed says so, and says whether an upgrade would fix it.

Publishing now validates the manifest against a pydantic schema and checks that
every declared apt package resolves in the pinned mirror -- so an unmirrorable
dependency is caught when it is published rather than when someone adopts it.

The manifest's "Holes" and "Prerequisites" sections are now a single
"Requirements" list. Each entry carries its own kind, so the difference that
matters is preserved without asking a publisher to pick the right heading:
permission, secret, and LLM entries are activated automatically by the adopting
agent before it asks anything, and the rest are worked through with the user
afterwards.

The generated
README is a real landing page: a hero graphic, an "Open in Minds" button, why
you care, how to use it, and ideas for making it yours.

Templates published in the older format keep working when adopted; the
publisher's next update migrates them.

Fixed a bug that would have failed every real publish: the manifest validator
located its schema module by indexing a fixed number of directories up, which
raised `IndexError` in the shallow temporary directory the assembly script
actually snapshots it into. It now searches the sibling copy first and then
walks ancestors. Caught by running the flow in a real workspace container --
the unit tests passed because they happened to run from a deeply-nested path.

When publishing atemplate, the agent now renders the generated README into
a preview tab and asks you whether it reads like a good description of what you
built -- before anything is pushed. If it does not, it rewrites and shows you
again. You review the page as a page, not as raw markdown in chat.

Publishing now starts by asking where it should go: it explains that the code
goes on your own account on a code-hosting platform, asks whether you have a
GitHub account, and either gets you connected or points you at the free signup
-- before it spends your time on the interview and a full assembly. Preferring
a different platform is a fine answer; the agent will tell you it has to work
out that platform's git authentication first rather than promising a publish it
cannot finish.

Titles and descriptions with quotes or colons no longer break a publish. The
manifest's front matter is YAML and those fields are your own words, so a title
like `The "Daily" Digest: v2` produced a file no YAML parser would read -- and
the same went for the generated welcome skill in the published template,
which would have failed to load at all. Both are now emitted as quoted YAML
scalars, and the skills say to do the same for any hand-edit.

Adopting atemplate installs what it declares with ordinary commands rather
than a special convergence step. The workspace's apt sources are already pinned,
so bare package names resolve at your snapshot rather than the publisher's, and
env-converge already captures whatever gets installed -- so it lands in the
environment record and survives a rebuild without any extra bookkeeping.
