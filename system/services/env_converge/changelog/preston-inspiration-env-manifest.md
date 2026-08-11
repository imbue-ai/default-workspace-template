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

How strictly a manifest is read depends on the format it declares. A manifest
in the format this workspace knows is read strictly: an unrecognised key there
is a typo in something we just wrote, and quietly dropping `apt_packages`
instead of `apt` would mean quietly not installing anything. A manifest
declaring any other format is read for what it does contain -- keys this
workspace has never heard of are set aside rather than failing the whole read,
so a template published by a newer workspace is still usable for everything it
shares with this one. Whatever was set aside is listed by name, and the publish
validator prints it, so a partial read is something you are told about rather
than something you discover later in a half-built environment.
