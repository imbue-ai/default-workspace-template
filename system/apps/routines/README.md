# routines (the app)

The Routines app: one window over everything in the workspace that runs on a
schedule. It is the user's view of the routines library
(`system/libs/routines`); the design is `docs/system/specs/routines/spec.md`.

- **`/`**, the list: a summary strip (how many routines, how many need
  attention, the next run) and one row per routine (title and sentence, the
  schedule and next run, a health pill, the last run and its summary),
  problems first, grouped by the app a routine belongs to. A field at the top,
  "Describe a routine to add", hands the sentence to the shell as a new chat
  ("Add a routine: ..."), so the agent writes the definition with the
  `manage-routines` skill.
- **`/<id>`**, a routine: the definition in plain words, Run now, On/Off,
  Change schedule (a number and a unit, a time, weekdays, a day of the month,
  previewed as the sentence it will read as), Fix it (opens a chat briefed on
  the failure, through `system/scripts/message_chat.py --create`), Remove, and
  the run history, each run a row that opens its page.
- **`/<id>/runs/<run id>`**, a run: the finished event's fields, the links it
  left, and the end of its log, as is.
- **`/<id>/runs/<run id>/log`**, the run's whole log, with the same
  navigation header as every other page.

One document serves every page; the page reads its path, reports it to the
shell with `shell:location`, and polls the API every 30 seconds (every 5 while
a run is going). The API is typed through `app_schema` and published at
`/api/schema`; every model is the library's, so the `routines` command's JSON
and the API's are the same shapes:

| Route | Answers |
| --- | --- |
| `GET /api/routines` | The listing. |
| `GET /api/routines/<id>` | One routine with recent runs. |
| `GET /api/routines/<id>/runs/<run id>` | One run with the tail of its log; `/log` the whole log as text. |
| `POST /api/routines/<id>/run` | Starts a manual run; 409 while one is going, 400 for a broken definition. |
| `PUT /api/routines/<id>/enabled` | `{"is_enabled": bool}`. |
| `PUT /api/routines/<id>/schedule` | The schedule fields; 400 with the reason, 409 in the library's words when the definition's commit is refused (the file stays written: the next definition write commits it). |
| `POST /api/routines/<id>/fix` | Opens the fix chat. |
| `DELETE /api/routines/<id>` | Removes the routine; 409 while a run of it is going, 500 with the reason when its state directory cannot be deleted whole (the routine stays). A removal whose commit is refused is still a removal (204, logged): the next definition write commits it. |

Every mutation goes through the library's service, which is what the
`routines` command runs. The manifest registers `minds:show-routine`, the
message the desktop delivers when a routine's notification is opened, as a
`show` handler on the routine's page.

Served on port 8050 by the `routines` program; `ROUTINES_APP_PORT`,
`ROUTINES_APP_HOST` and `ROUTINES_APP_PREVIEW` (a read-only boot beside the
live app, which answers every change with 403) are how a preview boots. The frontend lives in `frontend/` and builds
into `src/routines_app/static/`; see `system/apps/README.md` for the shared
frontend library and the npm workspace every frontend belongs to.
