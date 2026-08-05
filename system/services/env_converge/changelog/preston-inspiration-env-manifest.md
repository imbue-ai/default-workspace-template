Adds the inspiration-manifest schema: a pydantic model for `inspiration.toml`
covering an inspiration's identity, its derivation recipe, the activation
prerequisites an adopter must satisfy, the environment it declares (apt package
names, npm globals, uv tools, cargo crates, and carried `env.d` units), and the
lineage of inspirations it was built on.

The declaration shape deliberately mirrors the environment record: apt is a
bare name list, because versions are a function of the pinned snapshot
timestamp and so replaying names at the adopter's timestamp yields versions
consistent with the rest of their environment; the other sources are not
snapshot-pinned, so for them the recorded version is the pin. Cargo is included
because `~/.cargo/bin` binaries ride the backup as files, which makes the cargo
record matter precisely for inspirations and genuinely fresh homes.

The module imports only pydantic and the standard library, so the publish flow
can validate against the same schema from a worktree that has no virtualenv.
