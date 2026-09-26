# Element references

A user can right-click anything on their screen in the workspace and pick "Explain..." or
"Modify...". The chat then attaches a file named `REF-<id>.json` (for example
`REF-f38yat2o7y1.json`) to their message and the message calls it by that name: "Explain what
I attached in REF-f38yat2o7y1", "Change REF-f38yat2o7y1 to ...". The file arrives like any
other attachment: its absolute path is on the message's "See attachment here:" line, under
`data/uploads/`. Open it. "Copy reference" puts the same JSON on the clipboard, so it can
also arrive pasted inline as a fenced `json` block.

## What the file holds

One object under the key `element_reference`, describing the element as the page had it at
the click. Every field is present; a fact the page did not know is `null` or empty.

| Field | Meaning |
|---|---|
| `reference_id` | The `REF-<id>` the message calls it by |
| `app` | The app the page belongs to (`system_interface` is the desktop shell; `chat`; `getting_started`; anything else is an app under `system/apps/<app>/`); `null` when the page was opened outside the workspace |
| `window_id`, `desktop_id`, `client_id` | The window, desktop, and client the page was showing in; what `system/scripts/layout.py` addresses |
| `page_origin`, `page_path`, `page_title` | Where the page was: its origin, its path with the query, and its title |
| `tag`, `id`, `classes`, `attributes` | The element's tag, its `id`, its class list in order, and every other attribute (`data-*`, `href`, `src`, `style`, ...; a password field's `value` attribute is withheld) |
| `role`, `aria_label` | Its `role` and `aria-label` attributes |
| `selector` | A CSS selector matching exactly this element in the live page, or `null` when none could be built |
| `selection_text`, `selection_box` | The text the user had highlighted at the click, whole, and its rectangle |
| `input_value` | The value of an input, textarea, or select; `null` for a password field, whose value is never carried |
| `link_href`, `image_src` | The nearest enclosing link's URL; an image's URL |
| `pointer`, `bounding_box`, `viewport` | Where the click landed, the element's rectangle, and the page's size, all in CSS pixels |

## How to find the element

- Start from `app` and `page_path`: the page's source is that app's frontend (`system/apps/<app>/frontend/src/`
  for the built-in apps; the templates or static files of an app you built).
- Grep that source for the `id`, the classes (the first class is usually the one that names the
  element; the rest are often styling utilities), the `data-*` attributes, the `aria_label`, and
  the `role`. Those are what the markup carries, so they are what the source says.
- `selector` is for the live page (Playwright, the browser), not for grep: an
  `:nth-of-type(n)` in it counts siblings at the time of the click.
- `selection_text` and `input_value` are the user's own words: a highlighted passage or what they
  had typed. A long selection arrives in full.
- To look at the element or check a change, the window is `window_id` on `desktop_id`
  (the `manage-desktop` skill says how to refresh or focus a window); `pointer` and
  `bounding_box` say where on the page it was.
- When nothing matches (no `id`, no telling class, `selector` null), work from `page_path`,
  the tag, and the attributes, and ask the user if it stays ambiguous rather than guessing.
