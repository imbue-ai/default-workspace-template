The `macos_launch` CI job now also runs the new `macos-lifecycle` Playwright spec, which covers the macOS desktop app's windowless states (every window closed, then re-opened from the dock). Its timeout goes from 30 to 45 minutes, since each of those cases relaunches the app.

The job also stopped passing four renderer-contract spec filenames (`embed-flow`, `local-swap`, `recovery-redirect`, `landing-stopped-mind-restart`) that have matched nothing since those specs were deleted in the Mithril SPA migration.
