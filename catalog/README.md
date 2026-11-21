# The New Tab template catalog

`new-tab-templates.json` is what a workspace's Getting Started page shows under
"Start from a template": every published template and every published app it
can offer, and the rows ("shelves") that group them for browsing. The drawings
the template cards show live beside it under `thumbnails/`, and the icons the
app cards show under `app-icons/`.

The workspace does not ship this file. Its Getting Started app
(`system/apps/getting_started`) fetches it from a fixed URL -- by default the
raw GitHub URL of this file on the `mngr/new-tab-page` branch, set by
`SYSTEM_INTERFACE_TEMPLATE_CATALOG_URL` (the name kept from when the shell
fetched it, so nothing outside the workspace changes) -- reuses a fetched copy
for six hours, and keeps the last copy that parsed under
`data/.state/getting-started/template_catalog.json` so a machine that cannot
reach GitHub keeps showing what it last saw. A workspace on an older template
therefore picks up a refreshed catalog without an update, and a new field here
never breaks one: every model on the reading side ignores fields it does not
know, and only `slug`, `title`, `description`, and `repository_url` are required
of a template (`name`, `title`, and `repository_url` of an app).

Because the page reads the file on the `mngr/new-tab-page` branch, a change to
the catalog reaches workspaces only once it is committed there: the catalog
file, and every drawing and icon it points at.

## Format 1

```json
{
  "format": 1,
  "generated_at": "2026-09-07T00:00:00Z",
  "templates": [
    {
      "slug": "inbox-digest-review",
      "title": "Inbox Digest & Review",
      "description": "one or two sentences, what a card would say",
      "what_it_is": "several paragraphs, separated by blank lines",
      "author": "kanjun",
      "repository_url": "https://github.com/kanjun/inbox-digest-review",
      "thumbnail": "thumbnails/kanjun--inbox-digest-review.svg",
      "version": "v1",
      "updated_at": "2026-07-16T20:48:42Z",
      "required_accounts": [{"scope": "slack-api", "permission": "slack-read-all"}],
      "required_secrets": [],
      "needs_ai": false,
      "apt_packages": [],
      "choices": [{"summary": "what is unresolved", "resolution": "what to do about it"}]
    }
  ],
  "apps": [
    {
      "name": "weather-desk",
      "title": "Weather Desk",
      "description": "one or two sentences, what a card would say",
      "author": "someone",
      "repository_url": "https://github.com/someone/weather-desk",
      "icon": "app-icons/someone--weather-desk.svg",
      "version": "1.2.0",
      "required_accounts": [{"scope": "slack-api", "permission": "slack-read-all"}],
      "required_secrets": ["OPENWEATHER_API_KEY"],
      "needs_ai": true,
      "apt_packages": ["ffmpeg"]
    }
  ],
  "shelves": [
    {"key": "popular", "title": "Most popular", "slugs": ["inbox-digest-review", "app:weather-desk"]}
  ]
}
```

- `thumbnail` is a path relative to the catalog file (an absolute URL is kept as
  it is); the app resolves it against the URL it fetched the catalog from. The
  page admits images from the catalog's origin and its own, so a drawing on any
  other origin is blocked and the card shows its placeholder: keep the drawings
  beside the catalog.
- `apps` lists the published apps, each read from the `app.toml` at the root of
  its published repository: `name` and `title` (`display_name`), `description`,
  `version`, the permissions it asks for (`[[permissions]]`, as
  `required_accounts`), the secrets it asks for (every variable of
  `[[secrets]]`, as `required_secrets`), whether it calls a model (`[llm]`, as
  `needs_ai`), and its apt packages (`[environment] apt`). `repository_url` is
  the published repository an agent installs it from, and `author` its owner.
  `icon` is the app's `icon.svg`, a path resolved the way `thumbnail` is. An
  app's card offers "Add to this workspace" (a chat started on
  `/install-app <repository_url>`) and "Create a new workspace with this".
- Slugs are unique, and so are app names. A shelf's `slugs` are its entries in
  display order: a template's slug, or an app's name behind `app:`. A slug never
  holds a colon, so the two cannot be confused. An entry that names no template
  and no app the catalog carries is dropped from the row, and a row left empty
  is dropped with it.
- A catalog with no `apps` lists none, and a page from before apps could be
  published keeps working on a catalog that has them: it never reads `apps`,
  and an `app:` entry is to it a slug no template carries, which it drops. So
  an older workspace never offers an app it cannot install. A future kind of
  entry is added the same way, behind a prefix of its own.
- The shelves are shown in this order, followed by an "All apps" row (when the
  catalog lists any) and an "All templates" row the page synthesizes. "Most
  popular" is curated by hand here, not measured.
- `format` must be `1`. A reader refuses any other value rather than guessing,
  so a breaking change to the shape gets a new format number.

## Refreshing it

The file was produced from the mind-sketches prototype's export of the
published templates (`inspirations.json`) by `build_catalog_from_export.py`,
which also copies each template's drawing:

```bash
python3 catalog/build_catalog_from_export.py \
    --export path/to/inspirations.json \
    --thumbnails path/to/thumbnails \
    --popular inbox-digest-review,plain-text-gtd,daily-digest,family-weekend-radar,spec-workbench,zenbox,lunch-assistant,thread-writer
```

A generator that reads every `minds-template` repository's `template.toml`
and `template.svg` directly is the intended replacement for that export; it
should write this same format.

## Listing an app

An app is listed from a checkout of its published repository, which holds its
`app.toml` and its icon at the root. No export is needed: without `--export`
the builder keeps the templates and shelves the catalog already has.

```bash
git clone https://github.com/someone/weather-desk /var/tmp/weather-desk
python3 catalog/build_catalog_from_export.py \
    --app https://github.com/someone/weather-desk=/var/tmp/weather-desk \
    --popular inbox-digest-review,app:weather-desk,plain-text-gtd
```

`--app` is repeatable; it adds the app, or replaces the entry of the same name,
and copies the icon to `app-icons/<owner>--<name>.svg`. The icon has to be an
`.svg` file of the checkout itself: one that is a link or a path leading out of
the checkout is refused, since the checkout is someone else's repository. The
apps already listed are kept. `--drop-app <name>` removes one, with its icon and every shelf entry
naming it. `--popular` rewrites the leading shelf and takes `app:<name>` beside
template slugs; leave it out to keep the shelves as they are. Any other shelf
takes an app by adding `app:<name>` to its `slugs` by hand, which a later run
keeps (a run from an export rebuilds a shelf's templates and keeps the app
entries it held). `--catalog <path>` writes another file than the shipped one,
with its drawings and icons beside it.
