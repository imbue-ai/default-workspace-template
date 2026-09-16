# Mind first-run onboarding

## Purpose and scope

This spec replaces the Mind desktop client's first-run experience: the Electron loading screen, the welcome splash, and the pages a new user passes through to create their first workspace.
It follows the `mind-onboarding` prototype in the `mind-sketches` repo (published at <https://imbue-ai.github.io/mind-sketches/prototypes/mind-onboarding/>) with the prototype's levers fixed at: intro *type out*, chat *default*, buttons *user side*, choices *table*, first tab *empty*, crossing *wash*.
The prototype is React; everything here is re-implemented in the app's own stack (a static Electron document, the Mithril SPA, Flask).

In scope:

- The Electron loading document: the new lockup, a typed two-line intro, a status line and progress bar, and a hand-off into the SPA.
- A new SPA *start flow*: a chat-style conversation that asks the existing first-run questions one at a time.
- The *creation page*: the page that shows a workspace being created, restyled as the tail of that conversation and shared by first-run and later creates.
- The *wash*: the transition from the creation page into the finished workspace.
- Persisted onboarding progress, so a quit part-way through resumes sensibly.
- Removal of the welcome splash and the nine-step creating walkthrough.

Out of scope (see [Out of scope](#out-of-scope)): the error-reporting consent screen, the app icon, the titlebar's home glyph after the first run, dark-mode tuning of the intro, and changes to the default-workspace-template.

Target audience: the developer implementing this in `apps/minds`.
Related: [`specs/electron-desktop-app/`](../electron-desktop-app/spec.md), [`apps/minds/docs/desktop-app.md`](../../apps/minds/docs/desktop-app.md), [`apps/minds/docs/embed-contract.md`](../../apps/minds/docs/embed-contract.md), and the behavior corpus under `apps/minds/behaviors/`.
`specs/minds-onboarding/concise.md` described the welcome splash this spec retires; it is superseded (see `uncertainties.md`).

## Terminology

- **Loading document**: `apps/minds/electron/shell.html`, the static page Electron shows before the backend is up, and again for the quitting and error takeovers.
- **Lockup**: the brand mark, a blue blot with three white rings beside the lowercase word "mind" (`mind-wordmark.svg` in the prototype, 577 x 139). It is the only brand drawing this spec uses; the prototype's cropped `mind-head.svg` is not needed.
- **Intro**: the film the loading document plays on an installation's first launch: the lockup resolves, two lines type in and out, the lockup travels to its parked position.
- **Parked mark**: the lockup at 16px tall, centered horizontally, vertically centered in the 38px titlebar band, where the SPA's start-route titlebar holds the same drawing.
- **Start flow**: the SPA route `/start`: the manifesto exchange followed by the first-run questions.
- **Creation page**: the SPA route `/creating/<create-attempt-id>`, the chat-styled page that shows a create attempt's progress.
- **Turn**: one entry in a transcript. **User turns** are right-aligned grey bubbles. **Agent turns** are left-aligned bare paragraphs streamed a character at a time.
- **Onboarding complete**: the installation-level fact that the user has been taken past the point where the start flow is useful: they have started creating a workspace, or chosen "I already have one (log in)" and signed in.
- **Intro seen**: the installation-level fact that the loading document has played the intro once.

## Expected behavior

### Launch: the loading document

The loading document paints a white page.
It has two modes, chosen by whether the *intro seen* marker exists (see [Electron](#electron)).

**Intro mode** (marker absent):

1. The lockup, 32% of the window width, resolves from blurred and invisible to sharp over 2000ms after a 100ms delay.
2. The first line, "Create an *intentional* life", types in under the lockup starting at 2150ms: one character every 20ms, each fading in over 80ms, linear.
   It holds for 1000ms once its last character has landed, then rises 3px and fades out over 250ms.
3. After a 500ms gap the second line, "An *honest software*", types in the same way, holds, and leaves.
4. After a 150ms gap the lockup travels for 500ms on an ease-in-out-quart curve, shrinking and sliding up until it is the parked mark.
5. The status line and progress bar appear centered in the window (see below).

The italic runs are set in italic; nothing else about them differs.
The lines are set in a serif; the family is a single CSS variable in the document with the fallback stack `Georgia, "Times New Roman", serif`.
Any click or keypress during steps 1 to 4 jumps straight to step 5.
When the intro starts, Electron writes the *intro seen* marker, so a quit during the film still counts as having seen it.

**Parked mode** (marker present, and every later launch):

The parked mark, the status line, and the progress bar appear at once.
This replaces today's maroon letter-fade splash.

**Status line and progress bar** (both modes):

Electron broadcasts `status-update` messages to loading windows: the coarse phases of startup ("Setting up environment...", "Installing packages...", then "Starting Mind...").
The document shows the latest phase in a status line centered in the window, with a thin indeterminate progress bar above it.
Electron also forwards every line of startup console output (the environment setup's and the backend's, as `status-log-line`) to loading windows, minus lines carrying a credential the app consumes itself (the one-time login code, the forward tokens; see `electron/startup-log.js`).
Under the status line a "Show details" toggle, closed by default, opens a scrolling console log of those lines, like the creation page's own log panel; the bar and the phase stay visible either way.
The log keeps the newest 400 lines.
All of this is hidden while the intro is playing and shown from step 5 on.
The quitting takeover keeps its existing status line behavior and shows the parked mark.
The error takeover keeps its existing layout and controls and shows the parked mark in place of the old wordmark.

**Hand-off**: Electron navigates the window to its first route only when the backend is ready *and* the intro has finished or been skipped.
The document tells the main process when that is (an `intro-finished` IPC message; sent immediately in parked mode).
When the route is the start flow, the SPA's first frame paints the same drawing at the same size and place, so the mark does not appear to move or flash.

Reduced motion (`prefers-reduced-motion: reduce`) plays no intro: the document opens in parked mode.

### Which route a launch lands on

The Electron startup router (`electron/startup-routing.js`, `decideStartupRoute`) gains one input, `isOnboardingComplete`, read from the backend's app-status probe, and its `welcome` outcome is replaced by `start`.
Precedence, first match wins:

1. Not authenticated to the local backend: `start` (a graceful fallback, as `welcome` was).
2. Onboarding not complete and no workspaces exist: `start`.
3. The error-reporting consent is unanswered: `consent` (unchanged).
4. Nothing restorable: `create` (the home page, unchanged).
5. Otherwise: `restore` (unchanged).

Whether an account is signed in no longer affects the decision.
An install that already has workspaces never sees the start flow, whatever its flag says.

In plain-browser mode there is no Electron router.
The SPA's home page redirects to `/start` when the bootstrap document says onboarding is not complete and the workspace list is empty and initial discovery has finished.
The start flow's titlebar mark is not a link, so this cannot loop.

### The start flow

The start route renders a bare titlebar: the traffic-light spacer or window controls, and the lockup centered, in brand blue (`#1717F0`), 16px tall, not clickable.
No home button, breadcrumb, bell, or bug button.
The content is a transcript column 720px wide, anchored to the top and scrolling, with 100px of air above the first turn and below the last, and 48px between turns.
Every new turn scrolls the column to the end.

**The manifesto exchange**, on a fixed clock from the route mounting:

1. At 150ms the user turn "Wait.. what is honest software?" rises into place over 280ms.
2. 1000ms after it lands, the agent turn streams at 12ms per character, each character fading in over 70ms: the heading "Honest Software:" and then five points, one per line, each behind a chevron toggle (the `Disclosure` component):

   - is 100% loyal to you
   - never sells your data
   - is fully transparent
   - is safe and secure
   - doesn't lock you in

   Opening a point shows a short explanation under it at once (nothing streams); the open state lives in the page, so a redraw keeps what the reader opened.
   The explanations are placeholder copy (`MANIFESTO_POINTS` in `models/startFlow.ts`) to be revised later.
3. 1200ms after the last character lands, a filled green button "Sounds great, let's continue" rises in on the user side.

Buttons in the flow follow the prototype: the recommended or only way forward is a filled button in the app's success green, and the quieter alternative is a ghost button in secondary ink.

Pressing the button replaces it with the user turn "Sounds great, let's continue" and starts the questions.
From here every turn is driven by the user's actions, and each agent turn begins 500ms after the action that caused it.

**Where to run.**
The agent asks:

> Let's create your first workspace. A workspace is your own virtual computer. You can run it on Imbue Cloud (recommended) or bring your own platform (custom).

250ms after the question lands, a two-column table rises in under it on the agent's side, at 14px, with a rule under the headings and no other borders:

| Imbue Cloud (Recommended) | Custom |
| --- | --- |
| 30 second setup | Runs on your computer, or in your own cloud |
| Runs even if your computer is off | Docker or Lima here; AWS, GCP, Azure or Vultr there |
| Accessible from mobile | You manage uptime, backups and cost |
| Shareable with other people | |

Each point carries a check glyph, filled in the recommended column and outlined in the other.
"Recommended" is the create form's existing accent chip, beside the heading.
Once the table has landed, the agent streams the prompt "How do you want to run it?", and then one row appears:

- At its right end, on the user side: a ghost button "Custom" and, to its right, a filled green button "On Imbue Cloud".
- At its left end, on the agent side and quieter (link-styled): "I already have one (log in)".

A question's quieter way out always shares the row with its buttons.

**On Imbue Cloud.**
The buttons are replaced by the user turn "On Imbue Cloud" with an undo control inside the bubble (see [Undo](#undo)).
If no Imbue account is signed in, the agent says:

> Imbue Cloud it is. A cloud workspace runs on our machines, so it needs an Imbue account.

followed, on the user side, by a filled green "Create an account" button and a ghost "I already have one" button.
Both open the existing browser sign-in modal (the shared `webLogin` model) with the modal's intro text set to "Create an account to run your workspace in Imbue Cloud." or "Sign in to run your workspace in Imbue Cloud." respectively.
Dismissing the modal leaves the question standing.
When the accounts store reports an account, the buttons are replaced by the user turn "You've signed in as <email>", the agent says "You're in." (after Create an account) or "Welcome back." (after I already have one), and the create is submitted (below) once the email is known to be verified.
If an account is already signed in when Imbue Cloud is chosen, the agent says "Imbue Cloud it is. You're signed in as <email>." and the same check runs at once.

**Email verification.**
The connector refuses a cloud workspace for an account whose email is not verified (a password sign-up is unverified until its link is clicked; an OAuth sign-in is verified), so the flow checks before it submits rather than letting the create fail.
The check is `GET /accounts/verification?email=<email>` (see [Backend](#backend)); a check the app cannot make does not block, and the create is submitted anyway so its own error can say what is wrong.
If the email is verified, the create is submitted.
If it is not, the flow sends the verification email (`POST` on the same resource; signing up sends none of its own, and the lease refusal that used to trigger the first send is what this check pre-empts), the create waits, and the agent asks, with the first line in bold:

> **You must verify your email**
> (click the link sent to <email>)

followed by one row: on the user side a filled green button "I verified it", and on the agent side the quieter "Send the email again".
While the question is open the flow polls the check every 3 seconds and advances by itself when the click on the link lands.
"I verified it" checks for up to 10 seconds (every 2 seconds); when the email is verified within that time the question is answered as below, and otherwise the press is recorded as the user turn "I verified it" and the agent re-asks: "Not verified yet. Click the link in the email, then press the button again." with the same row.
"Send the email again" posts to the same resource and the agent replies "Sent another email to <email>." or, when the server's cooldown suppressed it, "An email was sent to <email> recently. Check your inbox and spam folder."
When the email is verified, the question is answered by the user turn "I verified it" (whichever side found out), the agent says "Your email is verified.", and the create is submitted.
A verified answer has no undo, since the fact cannot be taken back; undoing the where-to-run answer above it drops the wait along with everything after it.

Submitting the cloud create builds the create form's model programmatically: apply the `remote` preset to the form defaults, take the default account, and post the same body the create form posts.
The form defaults are fetched once when the start route mounts, so the submit usually costs no extra round trip; if that fetch failed, the submit fetches them again itself.
On a 202 the flow moves to the creation page.
On any other response the agent turn shows the returned error and offers the where-to-run buttons again, so a refused cloud create (a quota, an unverified email, no capacity) can fall back to Custom.

**Custom.**
The buttons are replaced by the user turn "Custom" and the agent says "Your own platform it is."
The create form opens as a modal (see [The create form as a modal](#the-create-form-as-a-modal)) with the `local` preset applied and the advanced view expanded.
Submitting it moves to the creation page.
Closing it without submitting, by its close control or the backdrop, adds the agent turn:

> Looks like you closed the custom dialog without finishing. No problem — would you like your workspace on Imbue Cloud instead? You can always move it later.

with the same "Custom" and "On Imbue Cloud" buttons under it, and no table.

**I already have one (log in).**
Opens the browser sign-in modal.
On success the flow marks onboarding complete (the `complete` endpoint, and the SPA's own in-memory copy of the flag) and routes to the home page, which lists the account's workspaces, or shows the empty state with its Create button if there are none.
Dismissing the modal changes nothing.

#### Undo

A user turn that answered a question carries a small undo glyph inside the bubble, at 60% opacity, full on hover, with the tooltip "Change answer".
Pressing it removes that answer and every turn after it and shows the question's buttons again immediately, with no arrival delay.
Undo is available only before a create has been submitted.

### The creation page

The creation page is reached in two ways: from the start flow after a create is submitted, and from the existing create form after a later create is submitted.
Its route is `/creating/<create-attempt-id>` in both cases, as today.
From the start flow the move is a plain route change: the bare titlebar is replaced by the normal one on the next frame, with no animation.

**Frame.**
The page renders in the normal app frame: the home button, the breadcrumb with the workspace's name and the switcher chevron, the permissions, settings, and share buttons, the bell, and the bug button.
The titlebar takes the workspace's accent, read from the in-flight create attempt's row in the workspace list, which already carries the name and color.
Pressing the permissions, settings, or share button opens a small dialog: "Check back here after the workspace is created to manage its permissions." / "... to change its settings." / "... to share it.", with a Close button.
The buttons are not disabled.

**Transcript.**
When the page is reached from the start flow in the same session, the start flow's transcript is shown above, unchanged, and the creation turns append to it.
When it is reached any other way (a later create, a reload, a relaunch), the transcript holds only the creation turns.

The creation turns are:

1. A user turn summarizing the create request, one setting per line:

   ```
   Create a workspace with these settings:
   Name — <display name>
   Compute — <launch mode or cloud account>
   Backup — <backup provider>
   Region — <region, omitted when the mode has none>
   Machine size — <instance type, omitted when the mode has none>
   Template repository — <repository, with a github.com prefix and .git suffix trimmed>
   Branch — <branch, or "latest">
   ```

   The lines are built from the attempt's persisted request, which the attempt detail endpoint exposes (see [Backend](#backend)), so a reload shows the same turn.
   The same formatting function is used when the create form is submitted from the start flow, so the turn is identical whichever way the page was reached.
   For a cloud preset submitted without a form, the same lines apply.
2. The agent turn: "Setting up your workspace. This takes a minute or two. While you wait, here is what is going on, and what you will be able to do once it is up."
3. The reading material, 250ms after the agent turn lands, fading in as one block: five `Disclosure` toggles (`SETUP_SECTIONS` in `models/creationTranscript.ts`), closed by default:

   - What a workspace is
   - What is happening right now
   - What you can do with it
   - How your data is handled
   - Changing it later

   Each opens a paragraph and a link out; every link points at https://imbue.com/product/mind until the docs it belongs to exist.
   The copy is placeholder, to be revised later.
   The block appears on every creation page, not only the first run, and is already open (no arrival) when the page is reached by a reload.
4. The loading box, 500ms after the reading material: a bordered box spanning the full transcript width containing a small uppercase label "Setting up", the workspace's name beside its accent blob (the jelly mark the workspace list uses, breathing), a progress bar, the stage line, and a "Show details" toggle.
   The progress bar is the existing time-eased bar (`progressForElapsed` against the attempt's expected duration).
   The stage line is the existing status text from the operation poll.
   "Show details" expands the existing log panel inside the box, streamed over the existing log SSE; the panel keeps today's height cap (`max-h-[22vh]`) and scrolls.

**Ready.**
When the operation reports done, the bar fills, the agent turn "Your workspace is ready." streams, and 900ms after it lands the wash begins (below).

**Failure.**
When the operation reports failed, the agent turn reads "Could not create <name>: <error>", followed by the existing recognized-error guidance where applicable (the private-repository and git-authentication notices), and on the user side a filled green "Retry" button and a ghost "Dismiss" button.
Retry opens the create form as a modal prefilled from the attempt's record, exactly as `/create?retry=<id>` prefills the page today, and submitting it routes to the new attempt's creation page.
This is a deliberate change for later creates too, which today leave for the create page; the modal keeps the failed attempt's transcript in view.
Dismiss deletes the record through the existing endpoint and routes home.

**Interrupted.**
When the page finds a record with no live attempt (the app quit mid-create), the agent turn reads "The app closed while this workspace was being created. You can retry with the same settings or discard the partial workspace.", with "Retry" and "Discard" buttons wired to the existing retry prefill and discard endpoint.

**Gone.**
An attempt id nothing knows routes home, as today.

### The wash

The wash carries the app from the creation page into the workspace.
A disc of the workspace's accent color, positioned on the loading box's accent blob, grows over 864ms to a circle large enough to cover the window from that point (its radius is the distance to the farthest window corner), holds opaque until 1128ms, and fades out by 1440ms, on a 2400ms timeline.
At 950ms, while the cover is whole, the shell enters the workspace through the creating page's existing `enterWorkspaceFromRedirect` helper (which parses the operation's `redirect_url` and calls `shell.enterWorkspace`), so the workspace frame is what the fade reveals.
The disc is mounted at the shell level, above the titlebar and the modals, because the whole window has to be covered while the screen changes underneath it.
The wash runs for first-run and later creates alike, and in plain-browser mode.
Under reduced motion there is no disc: the shell enters the workspace when the ready turn has landed.
The titlebar's piece-by-piece entrance from the prototype is not included.

### Later creates

The Machines page's Create button and the `/create` route keep today's create form.
Submitting it lands on the creation page described above, with no preceding transcript.
Nothing else about later creates changes.

### Resuming

- Quit during the intro: the marker is already written, so the next launch opens in parked mode and lands on the start flow.
- Quit during the start flow before a create is submitted: the next launch lands on the start flow at its beginning.
  The transcript is not persisted.
- Quit after a create is submitted: onboarding is complete, so the next launch lands on the home page (or restores windows).
  The home page's create-attempt row leads to the creation page, which shows the interrupted view if the attempt died with the app, as today.
- Sign in from "I already have one (log in)", then quit: onboarding is complete; the next launch lands on the home page.
- Sign in from the cloud branch's account step, then quit before the create submits: onboarding is not complete, so the next launch lands on the start flow; choosing Imbue Cloud then skips the account question.

### The create form as a modal

The create form page component (`CreatePage`) gains an embedded mode so the start flow and the creation page's Retry can host it in a modal without a second copy of the form.
In embedded mode:

- The "Create a machine / Where should it run?" masthead is not rendered; the modal supplies the title "Create a workspace".
- The caller chooses the initial preset and whether the advanced view opens expanded.
- The caller may pass a retry prefill id, exactly as the `?retry=` query does on the page.
- A successful submit calls an `onSubmitted(operationId)` callback instead of routing.
- Everything else, including validation, the account requirement, the bring-your-own-key account modal, and error display, behaves as on the page.

The submit request itself moves out of the page component into the create model so the start flow's form-less cloud submit and the form share one function.

## Design and changes

### Assets and styling

- `mind-wordmark.svg` is copied from the prototype into `apps/minds/electron/assets/` (read by the loading document) and `apps/minds/frontend/src/assets/` (imported by the SPA).
  The loading document references its copy by relative path; the SPA inlines its copy through Vite (the built bundle is served under a prefix Vite does not know, so a URL import would not resolve).
  The SVG carries `feTurbulence` filters, so they are only ever transformed, never re-rasterized per frame: the intro moves the lockup with a transform, not by animating its width.
- The lockup's travel is measured, not written in CSS: the scale is the ratio of 16px to the lockup's rendered height, and the slide is the distance from its resting top to the parked top.
  Both are computed from the element's layout box and driven through the Web Animations API, re-aimed on resize, as the prototype does (Chrome does not interpolate a `calc()`-derived transform in a keyframe).
- The chat portions (the start flow and the creation page) use the app's existing type: the system sans stack and the `type-body` role, which match the default-workspace-template chat app's `--font-sans` and 14px body.
  Agent turns use a line height of 1.5 and preserve newlines.
  User bubbles are `bg-fill-subtle`, 18px radius with a 4px bottom-right corner, max 80% of the column.
- The two intro lines are the only serif text, set from one CSS variable in the loading document.
- The start route and creation page use the theme tokens throughout, so they follow the app's light or dark mode; the lockup stays brand blue in both.
  The loading document is light only, as today.
- Every start-flow and creation-page animation honors `prefers-reduced-motion: reduce` by resolving to its finished state at once.

### Electron

`electron/shell.html`:

- Replace the maroon surface and the "Minds" letter-fade wordmark with the white surface, the lockup, the intro, the parked mark, the status line, and the progress bar as described.
- Keep the quitting and error views, restyled for the white surface with the parked mark.
- Read the intro mode from the URL hash: the main process loads the document with `#intro` when the intro should play, matching how `#quitting` is passed today, so the document itself reads no files.
- Send `intro-finished` over the preload bridge when the film ends, is skipped, or was never shown.

`electron/intro-timing.js` (new, electron-free): the intro schedule as numbers and a pure function that derives when each beat starts, unit-tested under `node --test` like `startup-routing.js`.
The document loads it with a plain script tag (the renderer has no `require`), so the file exposes its exports on `window` in a browser and through `module.exports` under node.

`electron/main.js`:

- A new `intro-seen.json` marker in the data root beside `window-state.json`, `{ "has_seen_intro": true }`, written when the intro starts.
  Its absence means play the intro.
- `runStartupSequence` decides the mode before loading the document and passes it in.
- Startup routing waits on the loading window's `intro-finished` promise before applying the route.
  A window opened later (`openStartupRoutedWindow`) never plays the intro.
- `applyStartupRouting` loads `/start` for the `start` route; the `welcome` branch is removed.
- `computeStartupRouting` passes `isOnboardingComplete` from app-status into `decideStartupRoute`.

`electron/startup-routing.js`: the new input and outcome as specified under [Which route a launch lands on](#which-route-a-launch-lands-on).

`electron/preload.js`: expose the `intro-finished` send.

### Backend

`desktop_client/minds_config.py`: a persisted boolean `is_onboarding_complete` (default false) with a getter and setter, alongside the consent flag.

Writers of the flag:

- The `/api/v1/workspaces` create handler in `api_v1.py` sets it to true once an attempt has started, so any create submission counts whichever surface submitted it.
- `POST /ui/api/onboarding/complete` sets it to true; the SPA calls it after a successful "I already have one (log in)" sign-in.
- `GET /accounts/verification?email=<email>` answers `{"verified": bool, "email": str}` by running `mngr imbue_cloud auth is-verified --account <email>` (`ImbueCloudCli.auth_is_email_verified`); `POST` on the same resource re-sends the verification email and answers `{"sent": bool, "email": str}`. Both answer only for an account this install has signed in (409 otherwise) and 502 when the CLI fails.
- The app-status probe sets it to true when it finds the flag unset but any workspace known, so installs that created workspaces on an older build converge without a migration step.
  Mark this backfill with a `CLEANUP` comment: it can go once every supported install has launched a build that writes the flag.

`ui_api.py`, `_handle_app_status`: add `is_onboarding_complete`, computed as the flag or the backfill above.
When no config store is wired (minimal test apps) the field reads true, so such apps never route to the start flow.
The bootstrap document's seed (`_build_bootstrap_json`) carries the same value.
The SPA keeps an in-memory copy seeded from the bootstrap and flips it to true itself when it posts `complete` or receives a create's 202, so the home page's redirect never fires against a stale seed after an in-session completion.

`ui_api_onboarding.py`: add the `complete` route; remove `skip-account-setup`.

`desktop_client/state.py`: remove `is_account_setup_skipped`.
`app.py`: remove `/welcome/skip` and `_handle_welcome_skip`; register `/start` as an SPA route and drop `/welcome`.

`ui_api_create.py`:

- `LiveCreateAttemptDetail` and `RecordCreateAttemptDetail` gain a `request` field carrying the attempt's persisted request in wire form (display name, launch mode, cloud account, backup provider, region, instance type, repository, branch), so the creation page can rebuild its summary turn on reload.
  For a live attempt the record is read as it is today for the display name.
- Remove `onboarding_services` from the live detail, and `desktop_client/onboarding_services.py` with its tests, once nothing else reads it.

The `/api/v1/workspaces` create endpoint and the attempt status and log endpoints are unchanged.

### SPA

Routes (`frontend/src/router.ts`): add `/start` (`StartPage`); remove `/welcome` and `WelcomePage.ts`.

`views/shell/classify.ts`:

- A new context kind `start` for `/start`: hides everything but the centered mark.
- A new context kind `creating` for `/creating/<id>`, carrying the create attempt id, which the Titlebar renders exactly like a workspace context (crumb, switcher, tabs) and which `accentSourceForRoute` returns so `paintAccent` resolves the attempt row's accent.
  The `agent-` / `host-` id patterns are not widened: no iframe exists for an attempt, so `workspaceSurfaceIdFromPath` and everything that mounts a frame keep ignoring the creating route.
  `WorkspacesStore.toAgentScopedId` and `accentEntry` must pass a create attempt id through unchanged; the attempt row's `id` is already the attempt id.

`views/shell/Titlebar.ts`:

- Render the centered lockup and nothing else for the `start` kind.
- For a creating-attempt context, the three tab buttons open the "Check back here after the workspace is created ..." dialog instead of routing to the options overlay.

New start-flow files:

- `views/pages/StartPage.ts`: the route component; renders the transcript column and drives the flow model, and exports `transcriptTurns`, which the creation page reuses to render the flow's transcript read-only.
- `views/pages/start/transcript.ts`: the shared chat primitives, used by both the start flow and the creation page: `userTurn` (with the undo control inside an answered bubble), `agentTurn` (streamed through `streamedText`), `answerRow` (right-aligned buttons), `asideLink`, `choiceTable`, and the `TranscriptScroller` behind `scrollAnchor`.
- `models/startFlow.ts` (under `frontend/src/models/`): the conversation as an append-only list of entries with a pure reducer for answer, undo, redirect-after-dismiss, and account-observed events, plus the manifesto schedule as numbers.
  The model holds the transcript at module scope (like `webLogin`) so the creation page can render it when reached from the flow in the same session.
- `models/creationTranscript.ts`: builds the summary turn's lines from an attempt request, and formats the failure and interrupted turns.

`views/pages/CreatingPage.ts`: rewritten on the shared transcript primitives per [The creation page](#the-creation-page).
`views/pages/creating/OnboardingWalkthrough.ts`, `graphics.ts`, `symbols.ts`, `models/walkthrough.ts`, and their tests are deleted, along with the walkthrough's CSS in `style.css` (the `#onboarding`, `.gfx*`, `.onboarding-*`, `.cloud-wheel*`, `.connect-*`, `.publish-*`, `.devices-*`, `#theme-demo` blocks).

`views/pages/CreatePage.ts` and `views/pages/create/form-model.ts`: the embedded mode and the extracted submit function per [The create form as a modal](#the-create-form-as-a-modal).

`views/shell/Shell.ts`: mounts the wash layer (`views/shell/Wash.ts`) above the titlebar; the creation page raises it with the accent color and the blob's window coordinates, and the shell enters the workspace at the swap instant.

`views/pages/LandingPage.ts`: the plain-browser redirect to `/start` in the empty state when onboarding is not complete.

`style.css`: the start flow's animations (character fade, turn rise, chrome-mark substitution) and the wash keyframes, plus their reduced-motion overrides.

### Behaviors and docs

- `apps/minds/behaviors/home-page/home-page.feature`: the empty-workspace scenarios distinguish an incomplete onboarding (the start flow) from a complete one (the create form).
- `apps/minds/docs/desktop-app.md`: the startup sequence, the loading screen, and the first-window routing.
- `apps/minds/docs/overview.md` and `docs/user_story.md`: the first-run flow.
- `apps/minds/docs/testing-overview.md`: the removed release test (below) and the new unit suites.
- Changelog entry at `apps/minds/changelog/mngr-fancy-onboarding.md`.

## Edge cases and failure modes

- **Backend slower than the film**: the parked mark, status line, and bar stay up until the backend is ready; the route is applied only then.
- **Backend faster than the film**: Electron holds the route until `intro-finished`.
- **Environment setup fails during the film**: the error takeover replaces the film at once; its Retry keeps working.
- **Window closed during the film**: the startup sequence continues without a window, as today; the next window opened lands on the route in parked mode.
- **Deeplink during the start flow**: unchanged from today, a `minds://create` deeplink wins over onboarding and lands on the template stepper.
- **Sign-in completes while the modal is dismissed**: the accounts store still reports the account; the flow observes it on the next redraw and advances exactly as if the modal had reported it.
- **Sign-in from the account step, then the user picks Custom via undo**: allowed; the account simply exists, and the form's account select defaults to it.
- **Cloud create refused** (quota, unverified email, no capacity, network): the error is shown as an agent turn with the where-to-run buttons offered again; nothing is marked complete because no attempt started.
- **Create attempt row missing from the list momentarily**: the titlebar crumb shows the ellipsis placeholder it shows today for an unknown workspace, and the accent stays neutral until the row lands.
- **Two windows on the creation page**: each runs its own watcher and wash, as the creating page does today.
- **Reload on the creation page mid-create**: the summary turn is rebuilt from the attempt's request; the start-flow transcript, if any, is gone.
- **The attempt finishes while the page is not open**: the redirect happens on the next visit, as today.
- **Onboarding flag corrupt or unreadable**: `MindsConfig` already raises on an unreadable config rather than defaulting; the app-status probe fails, and the router lands on `start`.

## Testing

- `apps/minds/test/unit/startup-routing.test.js`: the `start` outcome and the `isOnboardingComplete` input.
- `apps/minds/test/unit/intro-timing.test.js`: the derived schedule (line start times, hold, travel, and the parked instant) from the numbers.
- `frontend/src/models/startFlow.test.ts`: the reducer under answer, undo, dismissed-modal redirect, account observed, and the manifesto schedule.
- `frontend/src/models/creationTranscript.test.ts`: summary lines for the cloud preset, a custom form, and a bring-your-own-key account; omission of region and machine size where the mode has none.
- `frontend/src/views/pages/StartPage.test.ts`: the rendered transcript (question, table, answers, the existing-account way out, the undo control, the read-only form) and the cloud create body; `CreatingPage.test.ts`: the recognized-error guidance and the failure turn's ids; `start/transcript.test.ts`: the transcript scroller.
- Python unit tests for the config flag, the app-status field, the `complete` route, the create-path write of the flag, and the attempt detail's `request` field.
- `apps/minds/test_creating_page_layout.py` is deleted; it verified the walkthrough fit the window.
- The behavior corpus's home-page scenarios are updated and their witnesses re-linked.
- Manual verification: `just minds-start` on a Mac against a dev env for the Electron intro and hand-off, and a plain-browser run for the start flow and creation page.
  Interactive timing is checked by eye and not crystallized into tests, per the repo's guidance on interactive components.

## Out of scope

- The error-reporting consent screen keeps its current place in the startup router, after the start flow, so a new user meets it on their second launch until a later pass folds it into the conversation.
- The app icon and dock icon stay the current maroon head.
- After the start flow exits, the titlebar's home button keeps its house icon and "Mind" label.
- The prototype's licensed fonts (Caslon Ionic, ABC Diatype) are not bundled.
- The default-workspace-template is untouched; the "empty first tab" is the workspace's own New Tab page.
- The prototype's titlebar and content entrance stagger after the wash.
- Persisting the start-flow transcript across a quit.

## Open questions

Resolved in conversation before this spec was written; recorded here so the decisions are visible: the intro lives in the loading document, the flow is first-run only but resumable, the create form is shared not duplicated, the frame turns normal at the loading box, the setup sentence names neither name nor color, sign-in is the existing browser flow, the copy says "workspace", and the disabled-looking tab buttons open a dialog instead.

Still open:

1. Exact copy for the three tab dialogs, the interrupted turn, and the Custom column's second row (the prototype's "AWS, lima and vultr" names modes the app does not offer as written).
2. Whether the start route should offer any way out other than the three answers (today's welcome splash has none either).
