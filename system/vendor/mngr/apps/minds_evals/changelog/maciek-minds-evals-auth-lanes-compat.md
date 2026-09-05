The driver brings a workspace up the way the current product does: it signs the workspace in first,
then creates the chat it drives.

A workspace boots with no chat. A chat binds to a **provider account** when it is created, and only
the sign-in mints one, so there is nothing to create a chat against until the credentials have been
posted. The driver therefore signs the workspace in through
`/api/claude-auth/submit-credentials`, keeps the account id that answer carries, and creates its
chat through `/api/agents/create-chat` -- named after the workspace host and bound to that account
-- before waiting for the chat to be ready and capturing the pre-turn-1 app registry. A create
issued the other way round is refused for want of an account, so the order is load-bearing rather
than cosmetic.

Failures are named separately, so a trial says which step it fell at: the sign-in's own three
reasons (no key to sign in with, an auth endpoint that never came up, credentials the workspace
refused), `could not create the workspace chat agent`, `the workspace chat agent never reached
WAITING`, and `the workspace chat never answered its welcome`. Each one that actually waited is
recorded with the preparation budget it ran under, so a dead workspace reads differently from a
merely slow one; a failure the driver could tell immediately -- having no key at all -- names no
budget, because there was none to run out.

The chat the driver creates is the workspace's first, which is the one the product gives `/welcome`
to. `conversation.jsonl` -- the eval's own turns, which the gates and the wordiness check read --
carries no trace of it; the judged transcript drops the `/welcome` trigger and keeps the greeting it
draws, as its first agent message. The driver waits for that welcome to be *answered* before it
sends turn 1. A new chat reports WAITING as soon as its agent process is up, which is before the
workspace has typed `/welcome` into it, and that window is as wide as the workspace takes to reach
the agent's input box. Turn 1 sent into it would race the delivery, and the greeting would then
arrive where turn 1's reply is read from -- so the greeting, not the agent's answer, would be the
graded reply to turn 1.

Two behaviours of the create are worth knowing when reading a trial log:

- A create that never reaches the workspace's system_interface is retried until the deadline, since
  that is the workspace still coming up. A create the workspace *refuses* is final, and the trial
  stops there with the refusal's detail logged rather than spending its budget on a chat that will
  never exist.

- A create whose answer is lost still made the chat, so the retry collides with it. That collision
  is resolved back to the chat the first attempt left behind, by the name it was created under --
  which is the same identity the workspace refuses a colliding create on. The agents listing carries
  names and states only (labels reach clients over the workspace's WebSocket, which this bridge does
  not read), so the name is what a chat is found by.

Both bring-up steps report what actually answered them. The workspace's endpoints refuse in JSON, so
an answer that is not -- a traceback page, a proxy's 502 -- is logged verbatim, with its status,
rather than dropped for failing to parse. A trial that dies signing in or creating its chat says
what it was handed. A create that is never answered at all reports the last thing that *was* said --
mngr's own account of why the bridge could not reach the workspace, rather than the bare status line
a curl that never connected prints in its place.

The one thing that never reaches a trial log is the credential itself. What answers a sign-in can
quote the request that carried the paste -- an unreadable body is reported by rendering the
validation error, which carries the input -- so the key is masked out of everything the sign-in
logs, before any truncation rather than after.

The sign-in names which refusal it hit, by status: a paste the endpoint could not read and an
account it could not write both come back with a detail, and only one of them is the trial's
credentials at fault.

The calls a trial has to explain a failure of -- the two bring-up steps and the send -- are read on
the status, not merely on the presence of a body. The workspace refuses in JSON everywhere, so a
body alone proves nothing. (The pollers behind the turn loop -- the chat's state, its event count,
a window of its events -- still read the body alone, because they retry on any answer they cannot
use and a status would not change what they do.)

- A turn counts as sent only when the workspace says it took it. A chat listed as ready can still
  refuse a send -- the listing is a live mngr discovery, while the message endpoint answers from
  the workspace's own agent map, which a create fills later -- and so can a harness whose daemon is
  still starting. Those clear on their own and are waited out; a send that never lands reports the
  workspace's own refusal, rather than the trial blaming the agent for a silence that was really a
  message never delivered. A send being waited out names each refusal in the trial log the first
  time it appears, so a workspace refusing every attempt reads as a refusal rather than as an agent
  taking its time.

- The sign-in gate waits for an endpoint that can report the auth state. Being signed out is a
  normal answer there, so the endpoint's error shapes all mean it cannot report at all, and the
  trial says the endpoint never came up instead of posting credentials into it and failing later.

Signing in restarts nothing: the paste mints an account of its own rather than overwriting the
workspace's shared login, and the account existing is the signed-in flag. The driver has no restart
to wait out between signing in and driving the chat.
