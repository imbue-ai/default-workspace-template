The New Tab page is redesigned around starting things, after the `minds-new-tab` prototype (`docs/system/blueprint/new-tab-page/plan-new-tab-page.md`).

- A search field leads the page. Typing swaps the sections for results: the machine's instances and "Open new" actions that match, the matching "Start something" tiles, and the matching templates, each shown only when it has matches.

- "Open new" lays its tiles out four to a row, each a quarter of the row wide: the apps whose manifests declare a `launcher_rank`, lowest first (the built-ins: chat, file viewer, browser, terminal), then every other app, wrapping onto further rows.

- "In this project" is shown only when the project holds something; "On this machine" is no longer on a project's resting page and is reached through the search field. The Everything view keeps its machine-wide table.

- "Start something": six intent tiles (build an app, start from a template, connect your data, set up a routine, delegate a task, make sense of a pile of stuff), each starting a new chat seeded with a plain-language prompt, except "Start from a template", which scrolls the page to the templates section (also from search results) and is disabled when no catalog is configured; "See more" reveals the next page of tiles ("Learn about Minds" and "Edit Minds itself"). Each tile's glyph is drawn duotone in a Minds brand-palette hue, as in the prototype. The prompt goes to whichever app declares an action with a `message` param, so the shell still names no app.

- "Start from a template": the published-template catalog by category, each category a sideways rail of cards with a paging arrow at either end, outside the scrolling area so the cards run to the rail's edge with nothing over them. The whole sliver beside the rail pages it, so the arrow costs no aim, while what lights up under the pointer is just a circle level with the card drawings. Followed by an "All templates" row. A card opens a detail dialog (the drawing, the write-up, what it needs, the repository) whose "Make it mine" starts a chat that adopts the template with `/use-template`, and whose other action asks for a new machine made from it (both stand down, like the prompt tiles, when no app takes a first message or the pane is already starting something).

- Both offers float off the page under the pointer, eased out over 300ms, with exactly one thing inside them growing: a template card's drawing grows with it, while a "Start something" tile holds still (it is mostly text) and grows only its glyph. The shadow is a tile's whole hover answer -- it no longer fills its background behind it -- and a tile that is standing down stays flat.

- The catalog is fetched by the shell from `SYSTEM_INTERFACE_TEMPLATE_CATALOG_URL` (a versioned JSON document, see `catalog/README.md`), reused for six hours, kept as a last good copy under `data/.state/system_interface/template_catalog.json`, and served at `GET /api/templates-catalog`. The page says "Loading templates..." until it arrives and "Failed to load templates." when nothing could be loaded; with no URL configured the section is omitted.

- The shell's `apps_updated` message and inventory document carry each app's `launcher_rank` and each action's `params` (the names), read off the registry.

- A shell state file write that fails because a regular file sits where its directory should be now raises the documented `ShellStateError` (the temp-file cleanup no longer replaces it with a bare `NotADirectoryError`).
