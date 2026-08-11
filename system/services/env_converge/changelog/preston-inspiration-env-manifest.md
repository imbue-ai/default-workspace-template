Adds the template-manifest schema: a pydantic model for `template.toml`
covering a template's identity, its derivation recipe, the activation
prerequisites an adopter must satisfy, the environment it declares (apt package
names, npm globals, uv tools, cargo crates, and carried `env.d` units), and the
lineage of templates it was built on.

The declaration shape deliberately mirrors the environment record: apt is a
bare name list, because versions are a function of the pinned snapshot
timestamp and so replaying names at the adopter's timestamp yields versions
consistent with the rest of their environment; the other sources are not
snapshot-pinned, so for them the recorded version is the pin. Cargo is included
because `~/.cargo/bin` binaries ride the backup as files, which makes the cargo
record matter precisely for templates and genuinely fresh homes.

The module imports only pydantic and the standard library, so the publish flow
can validate against the same schema from a worktree that has no virtualenv.

Parsing is strict, and the `format` field is a wall rather than a hint. The
only thing that parses a manifest is the publish gate, and everything it reads
it is about to write back out, so an unrecognised key is a typo worth failing
on -- `apt_packages` for `apt` would otherwise mean quietly installing nothing.
A manifest declaring a format this workspace does not write is refused with a
plain message naming both versions, instead of a validation dump.

That refusal is what stops an older workspace from re-publishing a newer
template: it would either stamp its own format on tables it never understood,
leaving a file the next reader cannot parse, or drop them and delete what the
template's author declared. Adopting is unaffected -- nothing on that path
parses a manifest, so an agent reads the TOML and makes what it can of it.
