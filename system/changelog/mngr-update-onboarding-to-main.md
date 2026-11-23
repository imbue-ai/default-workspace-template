The desktop-interface contracts (`docs/system/blueprint/desktop-interface/contracts.md`, section 7) list the `opensWorkspaceSettings` flag the shell now puts in `shell:handshake`.

The shared frontend library (`system/libs/workspace_ui`) gains a coach mark component, `components/CoachMark.ts`: a bubble with an arrow and a "Got it" button that a page points at one of its own controls. Its README lists it with the other shared components.

The check that keeps the retired word "application" out of prose (`system/test_meta_ratchets.py`) no longer counts "job application", which names no app.

The new `onboarding_activity` library joins the uv workspace and the root project's dependencies, and `docs/system/blueprint/desktop-interface/contracts.md` describes the tour record's `is_finished` field and the `data/.state/onboarding_activity/` marker directory.

The same contracts (section 7) list the new page message `shell:show-coach-card` with its two places (a chrome control, or the agent's avatar), the coach card it shows, and the `showsCoachMarks` flag of `shell:handshake`. In the shared `workspace_ui` library the app contract gains that message and that flag, `embed.ts` the `coachMark` of `minds:open-share-settings`, and the new `onboarding_pointers.ts` the words two onboarding pointers show.

The new Onboarding checklist app (`system/apps/onboarding_checklist`) joins the uv workspace, the root project's dependencies, the npm workspace and the image's manifest layer, with its supervisord program (`onboarding-checklist`, port 8070) and its built bundle ignored by git. The contracts list its manifest row, the manifest key `seeds_shortcuts`, the chat's `onboarding:show-new-chat-pointer` handler, and how the tour's end opens and places the checklist; `test_app_manifests.py` holds the app to the built-in rules.

The contracts also list the page message `shell:agent-working {isWorking}`, which the tour's start reads beside the avatar's status, and the app contract in `workspace_ui` gains it (`connection.agentWorking`).

They list `shell:first-conversation-step {isPending}` the same way (`connection.firstConversationStep`), which holds the tour's start while a new workspace's first conversation follows its script, and the workspace app model's contracts name the `is_first_conversation_pending` the chat's `chats_updated` push carries.
