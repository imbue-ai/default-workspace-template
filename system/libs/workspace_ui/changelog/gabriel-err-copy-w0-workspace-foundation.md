A failed request inside a workspace is now told the same way everywhere: a sentence of ours on screen, and the status and the server's own text behind a collapsed "See details".

- One reader for every failed request, REST helper or chat request alike: a route's `user_facing_message` (a sentence written for people, shown as written), else its `detail` (a reason, kept behind "See details"), else no answer (nothing answered, or a gateway status with no reason of the server's), else nothing at all.

- One function words it, from what the screen says failed: a request nothing answered says "The workspace didn't answer. Try again in a moment." for a read and "The workspace didn't answer. This may not have gone through." for a change, never prefixed with what failed; a refusal with a reason says what failed ("This window couldn't be closed."); anything else says what failed, then "Try again.". These replace "HTTP 500", "request failed (HTTP 503)", "could not reach the workspace", "unknown error" and the gateway sentence that claimed "nothing was changed", which the workspace cannot know: Imbue Studio's forward proxy can send a gateway status after the request arrived.

- A failure the server did not explain, and one it wrote a sentence about, owes the person help beyond its own words, so its note stays on screen. A request nothing answered and a refusal with only a reason do not, nor does a sentence whose answer says `routine: true` (ordinary operation the sentence fully explains, such as an app still starting, so nothing is broken). A `detail` that only repeats the sentence stays out of "See details", which then holds the status alone.

- A screen not yet worded this way keeps showing the server's own reason: a failed request's error message, and `describeRequestError`, are still the route's `detail`, with our sentence only where the route gave none.

- A new piece for the screens that adopt it: `SeeDetails`, also drawn by `NoticeDialog` from its new `details`.

- The toast stack (`components/toasts.ts`) draws lasting toasts: a toast that stays until dismissed, with a close control, a collapsed "See details", and its buttons ("Try again", ...), which the queue (`models/toasts.ts`) holds at most three of. A button dismisses its toast once pressed. The queue replaces a toast that says the same as one already up (message and details) rather than stacking it. A plain toast takes no press unless the page sets `closesPlainToasts`, which gives it a close button, or it carries details: it then shows "See details" and a close button, and stays once the details are opened (`ToastQueue.hold`, through `Toasts`' `onHold`).

- `requestJson`, `getJson` and `postJson` throw the same `HttpError` for a refusal and for a request nothing answered, a GET read as a read and any other method as a change.

- A download that fails reads like the rest of the error copy: `Couldn't download "notes.txt".`, followed by the server's own words when it answered in plain text (the chat's image lightbox, the avatar chooser and Files share it).
