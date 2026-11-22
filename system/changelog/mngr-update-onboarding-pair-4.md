The new `onboarding_activity` library joins the uv workspace and the root project's dependencies, and `docs/system/blueprint/desktop-interface/contracts.md` describes the tour record's `is_finished` field and the `data/.state/onboarding_activity/` marker directory.

The same contracts (section 7) list the new page message `shell:show-coach-card`, the coach card it shows, and the `showsCoachMarks` flag of `shell:handshake`. In the shared `workspace_ui` library the app contract gains that message and that flag, and `embed.ts` the `coachMark` of `minds:open-share-settings`.
