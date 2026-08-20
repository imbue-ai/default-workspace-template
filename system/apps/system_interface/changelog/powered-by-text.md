The harness now declares its own composer credit text, and claude declares none.

`HarnessCatalog.powered_by_label` (a bare product name, e.g. `"Claude Code"`) became `powered_by_text`: the exact string to render, prefix included. Codex declares `"Powered by Codex"`, pi declares `"Powered by Pi Coding"`, and claude declares `""`.

The `"Powered by "` prefix was previously hardcoded in the frontend, which made "no credit for this harness" unexpressible — the spec could only choose the noun, never whether the line appeared. Now `PoweredByCredit` renders the string verbatim and renders nothing for `""`, so opting out is a spec change rather than a frontend branch.

`GET /api/agents/<id>/powered-by` keeps its `label` wire field and now carries that verbatim text, `""` included. `""` is a real cached answer, not a missing one, so the frontend renders nothing without re-fetching; the existing "not loaded yet / proto-agent 404" path is unchanged and still shows nothing.

Net effect in the UI: claude agents lose the "Powered by Claude Code" line under the composer; codex and pi are unchanged.
