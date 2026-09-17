- `just registry-dsn <tier>` prints the `export MINDS_HOST_POOL_DSN=...` line for a tier's box registry, for `eval` after activating the `<tier>-infra` root; the bare-metal recipe comments now say dev/ci box commands run from that registry activation.

- The minds-justfile and minds-dev-workflow skills now cover the dev box registry activation, the `employee` Vault role, and the WireGuard operator onboarding steps.
