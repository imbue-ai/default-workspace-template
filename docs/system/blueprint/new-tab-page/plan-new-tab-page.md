# The New Tab page: search, "Start something", and "Start from a template"

## Overview

- The New Tab page (`system/apps/system_interface/frontend/src/views/NewTabLauncher.ts`,
  the launcher panel the dock shows in any pane with nothing else in it) is today a
  strip of "Open new" tiles over two tables of the machine's instances. It answers
  "which existing thing do you want in this pane?" well and "what could you start?"
  not at all. This blueprint redesigns it after the `minds-new-tab` prototype in
  `imbue-ai/mind-sketches`: a search field on top, the "Open new" tiles and the
  project's own tab list under it, then two offers of things to start -- six
  hardcoded intents ("Start something") and a catalog of published templates by
  category ("Start from a template").
- The catalog is not compiled into the workspace. The shell fetches a versioned JSON
  file from a fixed URL, caches the last good copy on disk, and serves it to the
  page over a same-origin route. The first catalog is committed to this repo under
  `catalog/` and fetched from its raw GitHub URL; when the branch merges the URL
  moves to `main`, and a generator that reads every `minds-template` repo's
  `template.toml` can replace the hand-made file without touching the workspace.
- Both offers land in a chat. "Start something" tiles and a template's actions
  start a new chat whose first message is a prompt, so the chat app's `new` action
  gains an optional `message` param. The message rides `mngr create --message`,
  the same delivery `/welcome` uses, and is carried on the provisional record of a
  chat that first has to wait for a sign-in, so it is sent only once the chat
  actually launches.
- The shell keeps naming no app (its project ratchet holds that). The two places
  the page needs to know something about the apps are declared by the apps
  themselves, in their manifests: a `launcher_rank` puts an app's tile among the
  leading "Open new" tiles (the built-ins declare chat 10, files 20, browser 30,
  terminal 40), and an action with a `message` param is one the page can seed a
  prompt into (the chat's `new`). The registration script copies both onto the
  registry row, and the shell's `apps_updated` carries them to the page.
- Nothing about the dock's contract changes: the panel is still `launcher`, the
  tiles still carry `data-launch="<app>:<action>"`, the rows still carry
  `data-address`, and the sections still carry `data-section`. The minds desktop
  app's e2e helpers (in the mngr repo) and this repo's own Playwright suite find
  the page by those markers.

## Expected behavior

### The resting page

From top to bottom, inside the same `max-w-4xl` column the page uses today:

1. **Search field.** A full-width text field, placeholder "Search apps, chats, and
   templates". Typing anything swaps the sections below for search results (see
   below); Escape or the trailing clear button empties it and brings the resting
   page back.
2. **"Open new".** The eyebrow keeps its name and its "Starting..." state. The
   tiles are one list, four to a row, wrapping past four. The apps whose
   manifests declare a `launcher_rank` come first, lowest first: chat, file
   viewer, browser, terminal for the built-ins, skipping any the machine has not
   registered; every other openable app follows in registry order, so the first
   app a user adds starts a second row. Every tile is the same pill it is today,
   a quarter of the row wide, running the app's primary action.
3. **"In this project".** The active project's tab set, as today (recency-sorted,
   with the per-app filter menu). Omitted entirely when the project holds nothing,
   so a brand-new project's page goes straight from "Open new" to the offers. On
   the Everything view the table is the whole machine (headed "On this machine"),
   as today, since that view has no tab set of its own.
4. **"On this machine"** is no longer on the resting page of a project. It appears
   only in search results (below), where it is the machine-wide table.
5. A dashed rule.
6. **"Start something".** A three-column grid of bordered white tiles, each with a
   glyph, a title, and a sentence. Six are shown at first; a "See more" button at
   the lower right reveals the next six, and stays until every tile is shown (with
   eight tiles today it disappears after one press). Clicking a tile starts a new
   chat whose first message is the tile's prompt, except "Start from a template",
   which scrolls the page to the templates section. The tiles, in order:

   | Title | Description | What it does |
   |---|---|---|
   | Build a new app | Work through it together, one stage at a time, with the choices laid out for you. | chat seeded with a build-an-app prompt |
   | Start from a template | Adopt something another person already built and make it yours. | scrolls to "Start from a template" |
   | Connect your data | Link an account you already use, so your apps and chats can work from what is in it. | chat seeded with a connect-an-account prompt |
   | Set up a routine | Something that runs on its own schedule, like a briefing every morning. | chat seeded with a routine prompt |
   | Delegate a task | Hand something over and walk away. It comes back when the work is done. | chat seeded with a delegation prompt |
   | Make sense of a pile of stuff | Point at files, an export or an inbox and get something you can actually read. | chat seeded with a make-sense-of-data prompt |
   | Learn about Minds | Have Minds teach you about all of its different capabilities and features. | chat seeded with a tour prompt (behind "See more") |
   | Edit Minds itself | Change the interface, theme, chats, etc--Minds can modify itself! | chat seeded with a change-Minds prompt (behind "See more") |

   Each tile's glyph is drawn duotone in one of the Minds brand-palette hues (the
   hue darkened for the stroke, a wash of it for the fill); the tile itself stays
   a white card.

   The prompts are plain-language requests written so the mind's own skills
   (`build-app`, `latchkey`, `manage-scheduled-tasks`, `launch-task`,
   `fetch-process-show`, `do-something-new`) match on them; they name no skill.
   With no app on the machine whose action takes a `message`, the tiles are
   disabled and say so in a tooltip.
7. **"Start from a template".** One eyebrow over the whole catalog, then one row
   per category: a heading (the catalog's shelf title) and a sideways rail of cards.
   The rows are the catalog's shelves in the catalog's order ("Most popular" is
   the first shelf of the shipped catalog), followed by a synthesized "All
   templates" row holding every template. A card is the template's drawing in a
   3:2 frame, its title under it, and "by <author>" under that; no description on
   the card. The rail shows three and a half cards, so the cut-off card says there
   is more; a right arrow overlaying that card pages the rail one visible width to
   the right, a left arrow appears once the rail has scrolled and pages it back,
   and each hides at its end (each arrow is a full-height strip at the rail's end
   that fades the rail out under it, so the whole edge is the target). The rail
   also scrolls freely with the trackpad. Clicking a card opens the template's
   detail dialog.

### The template detail dialog

A modal over the page: the drawing large, the title and byline, the template's
full write-up as paragraphs, a "Needs" list (accounts to connect, an AI model, keys,
system packages; omitted when there is nothing), a link to the repository, and two
actions:

- **Make it mine** (primary): starts a new chat in this project whose first message
  is `/use-template <repository url>`, so the mind adopts the template into this
  machine and walks the user through its requirements.
- **Create a new machine from this**: starts a new chat whose first message asks
  the mind to create a fresh Minds machine from the template's repository (the
  `minds-api` skill does that) and to walk the user through anything it needs.

Both close the dialog; the new chat replaces the launcher in the pane, as any tile
does. Both stand down exactly as the prompt tiles do: disabled, with the same tooltip,
when no app on the machine takes a `message`, and disabled while the pane is already
waiting on a create.

### Search

While the field holds a non-blank query the page shows, in this order and each
only when it has matches:

1. **"On this machine"**: rows for every openable app's primary action whose label
   or app name matches ("Open new terminal", with a "+" glyph), then every instance
   on the machine whose title or app name matches, in the same table style as the
   resting page.
2. **"Start something"**: the tiles whose title or description match.
3. **"Templates"**: the templates whose title, description, or author match, laid
   out in a grid of the same cards.

Matching is case-insensitive and token-based: every whitespace-separated token of
the query must appear somewhere in the item's text, so "open term" finds "Open new
terminal". With no matches at all the page says so under the field.

### The catalog

- The catalog is a JSON document at `system_interface_template_catalog_url` (the
  shell's config, `SYSTEM_INTERFACE_TEMPLATE_CATALOG_URL`; default the raw GitHub
  URL of `catalog/new-tab-templates.json` on this repo's `mngr/new-tab-page`
  branch). Its shape, format 1:

  ```json
  {
    "format": 1,
    "generated_at": "2026-09-07T00:00:00Z",
    "templates": [
      {
        "slug": "inbox-digest-review",
        "title": "Inbox Digest & Review",
        "description": "one or two sentences",
        "what_it_is": "several paragraphs, blank-line separated",
        "author": "kanjun",
        "repository_url": "https://github.com/kanjun/inbox-digest-review",
        "thumbnail": "thumbnails/kanjun--inbox-digest-review.svg",
        "version": "v1",
        "updated_at": "2026-07-16T20:48:42Z",
        "required_accounts": [{"scope": "slack-api", "permission": "slack-read-all"}],
        "required_secrets": [],
        "needs_ai": false,
        "apt_packages": [],
        "choices": [{"summary": "...", "resolution": "..."}]
      }
    ],
    "shelves": [{"key": "popular", "title": "Most popular", "slugs": ["..."]}]
  }
  ```

  `thumbnail` is a path relative to the catalog file (an absolute URL is kept as
  is); the thumbnails ship beside the JSON under `catalog/thumbnails/`. Slugs are
  unique. Every field but `slug`, `title`, `description`, and `repository_url` has
  a default, and unknown fields are ignored, so a newer catalog never breaks an
  older workspace. A `format` other than 1 is refused.
- The shell serves `GET /api/templates-catalog`. It answers `200 {"catalog": {...
  templates with "thumbnail_url" resolved to absolute URLs ..., "shelves"},
  "is_stale": false}` from a fetch made on demand and reused for six hours; when
  the fetch fails it answers the last copy it wrote to
  `data/.state/system_interface/template_catalog.json` with `"is_stale": true`;
  with no copy at all it answers `503 {"detail": "failed to load templates"}`.
  With the URL configured empty it answers `200 {"catalog": null}`, and the page
  omits the section. A fetch that fails is retried on the next request after a
  minute, so a machine that was offline picks the catalog up once it is not.
- The page fetches the route once per load, shows "Loading templates..." under the
  eyebrow until it answers, "Failed to load templates." if it answers 503, and
  retries on the next launcher mount after a failure. A card whose drawing does
  not load shows a generic glyph in the frame instead.
- The shipped catalog is the prototype's export, converted: the 42 published
  templates with a repository (the six playable samples have none to adopt and are
  left out, as is one whose title does not belong on a first-run surface), one
  entry per slug (the newer of two repos publishing the same slug), the prototype's
  shelves that still hold something, and a hand-picked "Most popular" shelf first. The conversion script lives beside the catalog
  (`catalog/build_catalog_from_export.py`) so a refreshed export drops in.

### The chat's `message` param

- `chat`'s `new` action accepts an optional `message`: the first message the chat
  sends once it is running. It rides `mngr create --message`, which delivers after
  the harness signals readiness -- the same path the `first` template's `/welcome`
  takes.
- A chat minted while nothing is signed in carries the message on its provisional
  record; the launch after sign-in (`POST /api/agents/create-chat` with `agent_id`)
  sends it then. A launch that names a reserved `agent_id` and a `message` is
  refused, like one that names a `name` or `project_id`: the reservation decides.
- A seeded chat does not claim the workspace's first chat: the `first` template
  (with `/welcome`) still goes to the first plain chat, so a user whose first click
  is a tile gets the tile's prompt and nothing else, and the welcome later.
- `POST /api/agents/create-chat` takes the same optional `message`, for callers
  that create chats directly.

## Changes

### `catalog/` (new, at the repo root)

- `catalog/new-tab-templates.json`: the shipped catalog (format 1).
- `catalog/thumbnails/<owner>--<slug>.svg`: the drawings, copied from each
  template repo (the prototype already pulled them).
- `catalog/build_catalog_from_export.py`: converts the prototype's
  `inspirations.json` export into the catalog document; stdlib only.
- `catalog/README.md`: what the file is, how it is fetched, how to refresh it.

### `system/apps/system_interface` (backend)

- `imbue/system_interface/config.py`: `system_interface_template_catalog_url`
  (default the raw GitHub URL above).
- `imbue/system_interface/template_catalog.py` (new): the pydantic models of the
  catalog document (`extra="ignore"`), the parse that skips a bad entry and refuses
  a bad format, the `TemplateCatalogFetcherInterface` with an httpx fetcher, and
  `TemplateCatalogStore`: in-memory copy with a freshness window, the on-disk copy
  under the shell's state directory, and `read()` returning the catalog, the stale
  copy, or nothing.
- `imbue/system_interface/app_context.py`: the store hangs off
  `SystemInterfaceState`; `main.build_production_state` and
  `testing.build_test_state` build it (tests default the URL to empty so nothing
  reaches the network).
- `imbue/system_interface/server.py`: `GET /api/templates-catalog`.
- `README.md`: the New Tab paragraph and a note on the catalog route.

### `system/apps/system_interface/frontend`

- `src/models/TemplateCatalog.ts` (new): the wire types, the fetch with its
  loading / loaded / failed / disabled state, and the pure helpers (the "All
  templates" shelf, the shelf resolution, the search match).
- `src/views/startSomething.ts` (new): the intents table (title, description,
  prompt, glyph) and the "See more" page arithmetic.
- `src/views/NewTabLauncher.ts`: the page as described: search field, two tile
  rows, the project table, the rule, the intents grid, the shelves with paged
  rails, the search results, and the detail dialog (its own component in
  `src/views/TemplateDetailModal.ts`). Keeps the exported pure functions the tests
  use and the markers the e2e suites find.
- `src/views/DockviewWorkspace.ts`: passes the catalog state to the launcher;
  `runActionInPane` already takes params, and the launcher now sends `{message}`
  for seeded chats. The tile split (ranked row / other row) and the prompt target
  (the first app with an action taking `message`) are pure functions in the
  launcher module; `models/Inventory.ts` carries `launcher_rank` and each
  action's `params`.
- `src/style.css`: only the rail's hidden scrollbar, which no utility expresses.
- Tests: `NewTabLauncher.test.ts` extended (tile order, the hidden tables, "See
  more", search results, the catalog states, the dialog's actions);
  `TemplateCatalog.test.ts` and `startSomething.test.ts` for the pure helpers;
  `TemplateArt.test.ts`, `TemplateShelves.test.ts`, and
  `TemplateDetailModal.test.ts` for the template components.

### The app model (`system/libs/app_manifest`, `system/scripts/forward_port.py`)

- `AppManifest.launcher_rank` (optional, at least 1); `RegistryRow.launcher_rank`
  and `RegistryAction.params` (the names); the registration script copies both
  (its writer gains integers and arrays of strings inside inline tables); the
  shell's `app_wire_json` carries them. `contracts.md` sections 2, 3, and 8 say
  so. The four built-in manifests declare their ranks.

### `system/apps/chat`

- `app.toml` and `docs/system/blueprint/workspace-app-model/contracts.md` row:
  the `message` param.
- `imbue/chat/models.py`: `ProvisionalChat.message`, `CreateChatRequest.message`.
- `imbue/chat/instances.py`: `MESSAGE_PARAM`, accepted by `new`, handed to the
  reservation or the launch.
- `imbue/chat/agent_manager.py`: `reserve_chat(message=)`,
  `create_chat_agent(message=)`, `_build_chat_create_command(initial_message=)`
  appending `--message`; a seeded chat skips the first-chat claim.
- `imbue/chat/server.py`: the create route passes the request's message through.
- Tests: the argv contract (`--message` accepted by the live CLI), the
  reservation carrying the message into the launch, the refusal of a message
  beside a reserved id, and the instances API's param handling.

### `system/apps/system_interface/imbue/system_interface/test_e2e.py`

- `_open_from_launcher` searches for the instance's app (typing its name into the
  search field) when its row is not on the resting page (a machine row in a
  project view).
- The filter test drives the machine table through the search field.
- New: the resting page hides the machine table in a project and shows the
  catalog's shelves from a fake catalog fetcher injected into the shell state
  (answering the configured URL, or nothing for the failed-to-load case); a
  template card opens the dialog and "Make it mine" creates a chat with the
  adopt message (against the stub chat-like app's create body).

### Changelog entries

`system/apps/system_interface/changelog/mngr-new-tab-page.md`,
`system/apps/chat/changelog/mngr-new-tab-page.md`, and
`system/changelog/mngr-new-tab-page.md` (the catalog and this blueprint).

## Out of scope, and follow-ups

- "Most popular" is a curated shelf in the catalog file, not a measured one.
- The catalog generator that reads `minds-template` repos is a separate task; the
  catalog URL moves from this branch to `main` when the branch merges.
- The minds desktop app's own e2e helpers keep working unchanged; nothing in the
  mngr repo changes for this page.
