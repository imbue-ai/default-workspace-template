The share gateway now serves a visiting agent's commands and files itself, on three routes beside its other agent routes: `POST /_agent/run` runs a command and streams its output as JSON lines, `POST /_agent/files/read` answers a file's contents, and `POST /_agent/files/write` writes a file.
Each is admitted by the agent's admission token alone, checked on every request against a grant with workspace scope, so nothing is signed and revoking the grant ends access at the next request.
A command is stopped, with everything it started, when its time runs out, when its caller's connection ends first, and when the gateway stops.

The `wsvisit` helper calls these routes instead of signing requests to owner-exec.
It also says why a command ended without an exit code, no longer gives up on a command that prints nothing for five minutes, and never runs a command a second time when it cannot tell whether the first attempt started.

A helper installed before this change keeps working: the owner-exec daemon keeps running, a workspace-scope grant still adds the agent's key to `authorized_keys`, and the join and `me` answers keep their `exec` fields.
Such a helper prints its notice to reinstall when it joins and when a request of its fails.

Minting an invite and revoking a grant with a session cookie are now refused when the request comes from a page of another origin, the rule the gateway already applies to every request for an app.
Before, another site's page could make a signed-in owner's browser mint an invite, though it could never read the code.
