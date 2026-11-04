Reverts the build-app plan recorder (#530).

Removes `system/scripts/imbue_plan_extra/` (the `write_plan.sh` wrapper and its
`build-app` prompt) and the exemption for `write_plan.sh` in
`agent_require_steps_pretool.sh`. A workspace that added its own prompt under
`prompts/` and a `write_plan.sh <flow>` line to one of its skills keeps both,
but the command now fails with "No such file or directory"; remove the line
from the skill.

Keeps `data/.imbue/plans/CLAUDE.md`, moves its note into an `AGENTS.md` beside
it that the `CLAUDE.md` imports, and keeps the `.gitignore` rules that
track both and the `data/README.md` line. Workspaces that ran the recorder
still hold run folders under `data/.imbue/plans/<flow>/`, each with `brief.md`,
`plan.md`, `meta.json` and `log`. Only `plan.md` carries its own warning;
`brief.md` and `log` both contain the calling agent's description of the
user's request. A nested `CLAUDE.md` is loaded only once an agent reads a file
in that folder, so a shell search across `data/` can still print matching
lines before any note is seen.
