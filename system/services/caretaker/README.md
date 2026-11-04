# caretaker

The weekly **Caretaker**: a background maintenance agent that checks the
workspace about once a week and opens a chat only when there is something
to say. It is **off by default**: the routine `caretaker`
(`system/routines/caretaker.toml`, every week at 3 am) ships switched off, and
the user turns it on from the Routines app or with
`uv run --no-sync routines enable caretaker`; `disable` switches it off again.

Unlike the other packages in `system/services/`, the Caretaker is a routine
rather than a supervised program: it has no `[program:*]` block, and its
cadence, catch-up after downtime and retry of failed runs come from the
routines library (`system/libs/routines/`).

`caretaker_check.sh` is the deterministic weekly check. It looks for:

1. services in a bad supervisord state (FATAL / BACKOFF),
2. fresh error output in the service logs since the last check,
3. a nearly-full disk,
4. OOM-guard sheds since the last check,
5. routines that are failing, overdue, broken, or waiting on the user.

With findings, it writes them to `data/.state/caretaker/findings.md` and wakes
the Caretaker agent through `routines wake caretaker` (the routine's own chat,
recorded on the run); with `--only routines` it looks at the routines alone,
the shape a lighter daily check takes. The
agent then follows the `caretaker` skill (`.agents/skills/caretaker/`), always
speaking to the user in plain, non-technical terms. With no findings it exits
quietly -- except on the very first run (no
`data/.state/caretaker/permissions.md` yet), when it wakes the agent regardless
so it can introduce itself.

Runtime state (permissions, run notes, the last-check marker) lives under
`data/.state/caretaker/`.
