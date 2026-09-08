# The New Tab template catalog

`new-tab-templates.json` is what a workspace's New Tab page shows under "Start
from a template": every published template it can offer, and the rows
("shelves") that group them for browsing. The drawings the cards show live
beside it under `thumbnails/`.

The workspace does not ship this file. Its shell (`system/apps/system_interface`)
fetches it from a fixed URL -- by default the raw GitHub URL of this file on the
`mngr/new-tab-page` branch, set by `SYSTEM_INTERFACE_TEMPLATE_CATALOG_URL` --
reuses a fetched copy for six hours, and keeps the last copy that parsed under
`data/.state/system_interface/template_catalog.json` so a machine that cannot
reach GitHub keeps showing what it last saw. A workspace on an older template
therefore picks up a refreshed catalog without an update, and a new field here
never breaks one: every model on the reading side ignores fields it does not
know, and only `slug`, `title`, `description`, and `repository_url` are required
of a template.

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
      "required_accounts": [{"service": "slack-api", "permission": "slack-read-all"}],
      "required_secrets": [],
      "needs_ai": false,
      "apt_packages": [],
      "choices": [{"summary": "what is unresolved", "resolution": "what to do about it"}]
    }
  ],
  "shelves": [
    {"key": "popular", "title": "Most popular", "slugs": ["inbox-digest-review"]}
  ]
}
```

- `thumbnail` is a path relative to the catalog file (an absolute URL is kept as
  it is); the shell resolves it against the URL it fetched the catalog from.
- Slugs are unique. A shelf names templates by slug; a slug a shelf names that
  no template carries is dropped from the row.
- The shelves are shown in this order, followed by an "All templates" row the
  page synthesizes. "Most popular" is curated by hand here, not measured.
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
