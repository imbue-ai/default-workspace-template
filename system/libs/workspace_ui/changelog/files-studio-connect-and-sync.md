The app contract and the embed contract carry what Files needs to open Imbue Studio's settings for the files shared from your computer.

- `shell:handshake` has an `opensLocalFiles` flag, read as false when the shell does not send it: whether the Imbue Studio chrome opens its settings for the files shared from the user's computer.
- `embed.ts` probes the contract v11 `minds:open-local-files` (workspace to embedder), which asks Studio to open its Permissions pane at Local files.
- A tooltip can carry an optional second line in quieter text under the first (`hoverTooltipAttrs(text, placement, { detail })`), for a detail worth a glance -- a reason, a time -- without stretching the first line. It shows at most two lines, ending in an ellipsis past that. A tooltip without one is unchanged.
- `laptop-off` leaves the shared icon set: Files greys out a share whose computer isn't answering instead, and nothing else drew it.
