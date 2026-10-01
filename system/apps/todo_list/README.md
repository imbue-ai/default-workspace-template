# To-do, to-do

Turns email, Slack and the calendar into a to-do list, laid out as a scrapbook page: three
taped notes (what you promised, what others asked, what is worth knowing), each item with a
hand-drawn tick-box, its due date and the messages it came from. A to-do can gather several
conversations (an email thread and a Slack channel on the same topic); each shows on its own
line with a peek at its latest message, and clicking the item opens every original message.
Each group is sorted most urgent first, and the top of the page counts what is late and what
is due today.

Ticking an item draws a marker tick, stamps it, and flies it into the progress badge; the badge
opens a list of what's finished, where an item can be put back. The trash icon takes an item
off the list without counting it as done. Both show an Undo.

State lives under `data/.apps/todo-list/` (`state.json`, seeded from `assets/seed.json` on first
use, and `theme.json`); every open window follows the others live over a WebSocket, and items
another process adds (`POST /api/items`) or a theme change (`POST /api/theme`: `accent`, the main blue;
`background`, the page; `heading`, the group tape colour, stepped lighter down the page) reach them the same way. An item's status is `open`, `done` or `drop`, and
`POST /api/items/<id>` with any of the three sets it (so Undo is `open`).

Item fields: `id`, `section` (`promise` / `request` / `know`), `summary`, and either one source
(`src`, `where`, `raw`) or a `sources` list of `{src, where, when, raw}`; optionally `due`
(`YYYY-MM-DD` or `YYYY-MM-DDTHH:MM`, local time) and `asked` (`YYYY-MM-DD`, when someone asked,
shown as "waiting N days"). The sample items in the seed give their timing relative to today
(`due_in_days`, `due_time`, `asked_days_ago`) so the demo list never goes stale.

The handwritten (Caveat) and display (Shrikhand) fonts ship in `assets/fonts/` (both SIL Open
Font License) so the page looks the same offline.
