Detail that `AGENTS.md` no longer carries now lives where it is used:

- `creation-principles.md` (new): default to a web view, preserve and surface raw data and its source, name things for what they do, and use a platform's internal APIs. `build-app` step 1, `fetch-process-show` and `do-something-new` read it before shaping a creation, and `crystallize-creation` names its slug by it.

- `chat-rooms.md` (new): reading a chat room and answering there.

- `playwright-fortress.md` (new): launching Fortress from Playwright, waiting for its first-boot install, the sandbox flag, and driving controls around its cursor glide. `apps/verify.md` and `web-frontend-testing.md` link it.

- `runtime-failures.md` (new): a command killed by memory pressure, a tool gVisor cannot run, and a `ModuleNotFoundError` from mngr.

- `report-built-in-issues.md` says when to report a built-in defect and when to submit a fix with `submit-upstream-changes`.

- `update-app` step 5 starts the change's test gate in the background, as `build-app` does, and step 3 says a changed app icon is built to `docs/system/app-icons.md`.

- `web-frontend-testing.md` states the `real_claude` mark, that the chat app skips those tests by default, and that naming a browser test file without `-m ''` or `-m browser` deselects it.
