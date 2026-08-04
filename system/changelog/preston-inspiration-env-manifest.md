Added the design plan for inspiration environment manifests
(`docs/system/blueprint/inspiration-env-manifest/`): a pydantic-validated
`inspiration-<slug>.toml` that declares what an inspiration's code needs from
the environment (apt, npm globals, uv tools, cargo crates, and carried
`env.d` units), validated against the pinned apt mirror when it is published
and converged into the adopting mind's environment at that mind's own pinned
snapshot timestamp.
