# caretaker

The weekly **Caretaker**: a background maintenance agent that checks the
workspace about once a week and opens a chat tab only when there is something
to say. It is **off by default** -- nothing runs until the `enable-caretaker`
skill writes its cron entry (`data/.state/cron.d/minds-caretaker`, installed
live into `/etc/cron.d/`); the `disable-caretaker` skill removes it again.

Unlike the other packages in `system/services/`, the Caretaker is cron-driven
rather than supervised: it has no `[program:*]` block, and its cadence comes
from the automations machinery (`system/libs/automations/`) -- an every-minute
cron tick through `run_job.sh` (`--every 7d --at 3`, catch-up after downtime,
retry of failed runs).

`caretaker_check.sh` is the deterministic weekly check. It looks for:

1. services in a bad supervisord state (FATAL / BACKOFF),
2. fresh error output in the service logs since the last check,
3. a nearly-full disk,
4. OOM-guard sheds since the last check.

With findings, it writes them to `data/.state/caretaker/findings.md` and wakes
the Caretaker agent via `run_automation.sh caretaker --template caretaker`; the
agent then follows the `caretaker` skill (`.agents/skills/caretaker/`), always
speaking to the user in plain, non-technical terms. With no findings it exits
quietly -- except on the very first run (no
`data/.state/caretaker/permissions.md` yet), when it wakes the agent regardless
so it can introduce itself.

Runtime state (permissions, run notes, the last-check marker) lives under
`data/.state/caretaker/`.
