`requestJson(method, url, body?)` in `models/http.ts` is the JSON API call with any method (a body only when given, never from the browser's cache), which `postJson` now calls; the Routines app reads and changes its routines through it.

`embed.ts` exports `isContractEmbedderMessageType(type)`, whether a chrome message's type is one the embed contract defines (and so one its endpoint dispatches to the observer); the shell's relay hands on the chrome's other messages (a notification's `destination`) itself.
