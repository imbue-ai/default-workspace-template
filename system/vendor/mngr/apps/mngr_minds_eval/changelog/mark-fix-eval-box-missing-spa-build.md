Fixed the eval box image building no UI, which left the noVNC desktop showing a "frontend not built" 503 page instead of Minds.

The image ran `pnpm run build:css`, but that script was deleted along with the desktop client's legacy JinjaX front end and its whole Tailwind chain. Because the step was written as `(pnpm run build:css || echo "build:css failed (non-fatal)")`, the missing script did not fail the build -- the image kept building green while compiling nothing.

Its replacement, `build:ui`, was never invoked here. That matters because `entrypoint.sh` execs electron directly rather than `pnpm start`, so the `prestart` hook that would otherwise build the bundle never runs, and the bundle's output directory (`desktop_client/static/ui`) is gitignored, so it is absent from the copied source too.

The image now runs `pnpm run build:ui`. This affects only the human-facing side of the box -- the desktop you watch a batch through. Eval trials themselves were never impacted: `launch` discovers the backend port from inside the container and drives the JSON API (`/api/v1/workspaces` and the create-operation poll) exclusively, and those routes are served independently of the SPA bundle.

The step is deliberately left best-effort, so a frontend build failure degrades the debugging view rather than blocking eval image builds for a UI the trials never use.
