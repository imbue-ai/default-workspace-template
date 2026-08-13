Hardened the raw `curl` downloads in CI against transient network failures, matching the retry treatment already applied to the copybara mirror fetch. Each affected download now passes `--retry ... --retry-all-errors --retry-delay ...` so a transient 5xx or a mid-transfer connection drop (curl exit 56, which plain `--retry` does not cover) retries instead of failing the step:

- the Vault CLI zip download in the minds deploy jobs (`ci.yml`)

- the GitHub OIDC token exchange in those same jobs (`ci.yml`)

- the built minds.app artifact download in the launch-to-first-message workflow (`minds-launch-to-msg.yml`)

Also set `Acquire::Retries=3` on the `apt-get update`/`install` calls in `ci.yml` and `release-tests.yml`; apt performs no retries by default, so a transient mirror hiccup would otherwise fail the job.
