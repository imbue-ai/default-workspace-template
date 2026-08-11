`xvfb` and `xclip` are now baked into the base image (`setup_system.sh`) instead
of arriving via the deferred env.d browser unit. `[program:xvfb]` execs Xvfb
directly at boot, so a snapshot, restore, or fresh boot that raced the deferred
install carried a service that could not spawn (`xvfb: ERROR (spawn error)`
from `supervisorctl restart all`). The unit's xvfb step remains as an instant
no-op for rootfses built from pre-bake images, so no rollout ordering is
required.

The env.d browser unit (`system/scripts/env.d/1000-playwright-fortress.sh`) now
honors `DWT_SKIP_BROWSER_UNIT=1` in the agent environment: environments that
never use the browser stack (the minds CI snapshot producer) skip the whole
unit, including the hundreds-of-MB Fortress download. The switch is
re-evaluated every boot per the env.d contract, so clearing it converges the
browser stack on the next boot.
