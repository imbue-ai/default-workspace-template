Fixed the opencode model bar showing only the logo (empty model) before the first turn.
The pre-turn-1 model comes from probing opencode's server, but the server starts after the
agent is tracked, so the probe at resolver-build time often missed it -- and the result was
cached, stranding the bar empty. The resolver now caches only a SUCCESSFUL probe (re-probing
until the server answers) and watches opencode's readiness marker, so the model appears as
soon as the server is up, without waiting for a turn.
