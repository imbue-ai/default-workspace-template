Decoupled the harness logo from the model bar so it never disappears. The logo is a pure
function of the agent's harness, but it used to render inside the model bar, which returns
null whenever the catalog/model-choice/agent isn't resolved (and the whole composer footer
is hidden for a proto-agent) -- so the logo blinked out during startup and model resolution.

The logo now renders through its own path: a new `GET /api/agents/{id}/harness-logo`
endpoint resolves the harness backend-side and returns its SVG, and a standalone HarnessLogo
component fetches it once per agent (cached, render-stable) beside the model bar. It shows as
soon as the agent is real (a proto-agent 404s -> no logo, as intended) and stays put through
model-choice churn. The model bar now renders only the model/effort/fast slots.
