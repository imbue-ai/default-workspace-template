# routines (the app)

The Routines app: one window over everything in the workspace that runs on a
schedule. It is the user's view of the routines library
(`system/libs/routines`); the design is `docs/system/specs/routines/spec.md`.

- **`/`**, the list: a summary strip (how many routines, how many need
  attention, the next run) and one row per routine (title and sentence, the
  schedule and next run, a health pill, the last run and its summary),
  problems first, grouped by the app a routine belongs to (the app whose
  directory holds its definition file, else the app its definition names). A
  field at the top,
  "Describe a routine to add", hands the sentence to the shell as a new chat
  ("Add a routine: ..."), so the agent writes the definition with the
  `manage-routines` skill.
- **`/<id>`**, a routine: the definition in plain words, Run now, On/Off,
  Change schedule (a number and a unit, a time, weekdays, a day of the month,
  previewed as the sentence it will read as), Fix it (opens a chat briefed on
  the failure, through the chat app's create route, using the workspace's chat
  client `message_chat`), Remove, and
  the run history, each run a row that opens its page. A routine an app owns
  (its definition file is under `system/apps/<package>/routines/`) names that
  app and says that only its schedule and its switch can be changed here: it
  has no Remove, and while the user's own schedule is in place of the app's the
  page shows the app's and offers "Back to the app's schedule".
- **`/<id>/runs/<run id>`**, a run: the finished event's fields, the links it
  left, and the end of its log, as is.
- **`/<id>/runs/<run id>/log`**, the run's whole log, with the same
  navigation header as every other page.

One document serves every page; the page reads its path, reports it to the
shell with `shell:location`, and polls the API every 30 seconds (every 5 while
a run is going). The API is typed through the app SDK (`imbue.app_sdk`) and published at
`/api/schema`; every model is the library's, so the `routines` command's JSON
and the API's are the same shapes, except that the app's row (alone, in the
listing and in the detail) carries four more fields, which say whose the
routine's definition file is:

| Row field | Meaning |
| --- | --- |
| `owning_app` | The app whose directory holds the definition file: the name the workspace's record of the app (`system/config/apps/<name>.toml`) gives the directory, else the directory's own name; `null` for a routine under `system/routines`. Read from where the file is, not from the definition's `app` field. |
| `is_schedule_only` | Whether the schedule and the on/off switch are all that can be changed here: `true` for a routine an app owns. |
| `has_schedule_override` | Whether the routine runs on a schedule the user gave it in place of its app's. |
| `app_schedule_text` | The app's own schedule in words while the user's is in its place; `null` otherwise. |

The routes:

| Route | Answers |
| --- | --- |
| `GET /api/routines` | The listing. |
| `GET /api/routines/<id>` | One routine with recent runs. |
| `GET /api/routines/<id>/runs/<run id>` | One run with the tail of its log; `/log` the whole log as text. |
| `POST /api/routines/<id>/run` | Starts a manual run; 409 while one is going, 400 for a broken definition. |
| `PUT /api/routines/<id>/enabled` | `{"is_enabled": bool}`. |
| `PUT /api/routines/<id>/schedule` | The schedule fields; 400 with the reason, 409 in the library's words when the definition's commit is refused (the file stays written: the next definition write commits it). For a routine an app owns the schedule is kept in the routine's state and the app's file is not rewritten. |
| `DELETE /api/routines/<id>/schedule` | Goes back to the schedule the routine's app gives it, dropping the one the user gave it; answers the row, the same when there was none to drop. 409 for a routine no app owns, 400 for one whose definition does not load. |
| `POST /api/routines/<id>/fix` | Opens the fix chat. |
| `DELETE /api/routines/<id>` | Removes the routine; 409 while a run of it is going, 409 for a routine an app owns (it is removed by changing the app) or whose id more than one file has, 500 with the reason when its state directory cannot be deleted whole (the routine stays). A removal whose commit is refused is still a removal (204, logged): the next definition write commits it. |

Every mutation goes through the library's service, which is what the
`routines` command runs. The manifest registers `minds:show-routine`, the
message the desktop delivers when a routine's notification is opened, as a
`show` handler on the routine's page.

Served on port 8050 by the `routines` program; `ROUTINES_APP_PORT`,
`ROUTINES_APP_HOST` and `ROUTINES_APP_PREVIEW` (a read-only boot beside the
live app, which answers every change with 403) are how a preview boots. The frontend lives in `frontend/` and builds
into `src/routines_app/static/`; see `system/apps/README.md` for the shared
frontend library and the npm workspace every frontend belongs to.
