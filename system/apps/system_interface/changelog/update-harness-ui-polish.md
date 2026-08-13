Polished the multi-harness chat UI in three ways, plus one config default.

The alt-harness launchers are now OFF by default. The `FEATURE_FLAG_ENABLE_OTHER_HARNESSES`
line was removed from `system/supervisord.conf`, so an unset flag resolves to false (the
server default already did this). When the flag IS enabled the three launchers now read
`New Claude agent`, `New Codex agent`, and `New Pi agent` -- the claude launcher's label
becomes explicit alongside the others. When the flag is off, the single claude launcher keeps
its plain `New chat` label and the codex/pi items stay hidden.

Removed the per-agent harness logos and replaced them with a non-clickable "Powered by"
credit. The three `icon.svg` files, the `HarnessLogo` component, and the `icon_svg` catalog
field are gone. `HarnessCatalog` now carries a `powered_by_label` (`Claude Code`, `Codex`,
`Pi Coding`), and the per-agent endpoint became `GET /api/agents/{id}/powered-by` returning
`{ "label": ... }`. A new `PoweredByCredit` component fetches the label once per agent (cached,
proto-agent 404 -> nothing shown) and renders `Powered by <label>` as a static span placed
immediately to the left of the "Open agent terminal" button, matching that button's font.

Made the signed-out refusals for Codex and Pi tell the user exactly how to sign in. The auth
check now carries per-harness `signin_instructions` appended to the refusal: Codex -> "Go to
New tab (+) -> New terminal -> run `codex`"; Pi -> "Go to New tab (+) -> New terminal -> run
`pi` -> type `/login`".
