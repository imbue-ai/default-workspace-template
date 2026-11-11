# A pass the careful flow dispatched for a critical app

Read this when `update-app`'s careful flow
(`.agents/skills/update-app/references/critical-app.md`) sent you to
`update-creation` to hand a critical app's change to a harden worker. No other
pass needs it.

A critical app (the shell, the chat, the terminal, or an app whose manifest
says `critical = true`) is an `app:` creation too, but it reaches
`update-creation` only from the careful flow, which calls into Steps 1-3 for
the orchestration core, creates the worker at approval on the branch the lead
already built up, and owns the go-live through the atomic update apply. When a
change touches a critical app, the **whole change** takes that flow: every
other creation it touched is edited on the same branch, rides the same pass,
and lands through the same apply.

What differs from an ordinary update pass:

- **The task file carries no `## Change origin` marker, and the worker runs no
  gate** (see "Critical-app handoff" in
  `.agents/skills/harden-worker/references/op-update.md`).
  The careful flow already got the user's approval through its live preview
  loop, so re-arming the gate would ask them to approve the same thing twice --
  and writing the marker anyway, or telling the worker in the task body to
  follow it, puts the gate back. Every other task file needs the marker.
- **The launch passes `--branch`**, so the worker checks out and extends the
  branch the lead already built up instead of branching anew. The careful flow
  has the command, and the `diff_base` its task file takes.
- **Step 4 is not yours here: do not merge or go live from `update-creation`.**
  The careful flow drives the live preview loop and the go-live through the
  atomic update apply, and tears the worker down itself.
