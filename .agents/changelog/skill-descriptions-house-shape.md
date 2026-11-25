The descriptions of 22 built-in skills follow the authoring guide's shape: what the skill does, when to use it and, where a neighbouring skill takes the case, when not to. They are in the third person and carry no steps, file paths or reply wording.

- `blueprint`, `devices`, `notify-user` and `submit-upstream-changes` gained the when-clause they lacked.

- `file-sharing` no longer promises Apple Notes and Photos imports it has no procedure for.

- `agentic-browser-fleet` says what the browser is for instead of routing every use through `connect-external-service`, which still comes first for reaching a service on the user's behalf.

- `caretaker` says it is only ever invoked by `/caretaker`.

- `do-something-new` no longer tells the agent to send a short confirmation and load the skill before replying further.

- Six descriptions carry the trigger that only `AGENTS.md` stated: `find-transcripts` (before saying an earlier or deleted conversation cannot be reached), `file-sharing` (files the user mentions are on their own computer), `connect-external-service` (a pasted link or a site to sign in to, but not a public page), `launch-task` (work long enough that the chat should not block on it), `update-app` (before editing an app's or service's code or supervisord config for any reason, the shared UI, layout and theme libraries included) and `manage-routines` (a scheduled job is a routine, never a supervisord program or a cron line).

- `service-processes.md` scans a new or changed service's log with `check-app-errors`.

The skills a restructure rewrites (`build-app`, `update-app`, `crystallize-creation`, `update-creation`, `heal-creation`, `update-self`) otherwise get their descriptions with that restructure.
