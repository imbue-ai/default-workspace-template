Generalize the Caretaker into a reusable "task agent". `scripts/run_task_agent.sh <skill>` spawns a singleton agent that runs any skill on a cadence -- creating it on the first run and, on later runs, clearing its chat and re-sending `/<skill>` so the skill runs fresh in an empty conversation. A reusable `task_agent` create-template means a new scheduled agent (e.g. a morning news digest) needs only a skill plus a scheduler entry, with no new template.

The Caretaker is now this script with its tailored template, scheduled directly in the seeded `scheduled_tasks.toml` (`bash scripts/run_task_agent.sh caretaker --template caretaker`); the old `run_caretaker.sh` wrapper is removed. How to set up an agent task is documented in the `manage-scheduled-tasks` skill and CLAUDE.md.

Simplify how the Caretaker stores the user's permissions. Instead of a `preferences.py`
script writing a `preferences.toml`, the Caretaker now keeps a single plain-language
`runtime/caretaker/permissions.md` that it reads at the start of every run and rewrites
a line of whenever the user changes their mind. The file's existence is what marks the
Caretaker as introduced, and the user can edit it directly any time -- plain yes/no
answers are all it needs. The `preferences.py` helper script is removed.

Also fix the in-workspace tab-blink, which flashed continuously. `surfaceHighlightedAgents()` runs on every discovery update (~10s poll) and re-armed the flash on any already-open, unacknowledged highlighted tab every time; it now gates the in-place re-flash on the highlight key, so a tab flashes once per genuinely new run. And simplify how the system interface gets labels: discovery already re-reads each agent's `data.json` into `certified_data` on every poll, so the snapshot carries current labels (including the per-run `highlight` bump) on its own -- the system interface now takes labels straight from the discovery event and drops the redundant local `data.json` re-read.
