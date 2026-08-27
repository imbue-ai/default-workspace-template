# Drop key_available from the browser-create response

`POST /api/browsers` no longer returns `key_available`. The field reported whether an
Anthropic API key was present, which mattered only for the browser service's LLM-driven
`task` verb -- that verb is gone, and nothing in the browser needs a key any more. The
frontend never read the field; only the `createBrowser` test fixture mentioned it, and that
fixture is updated to match what the daemon actually sends.

No user-visible change to the workspace UI.
