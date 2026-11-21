On a phone, the workspace no longer flashes its home screen before opening on a window. Until the page knows where it lands, the bar reads "Opening…" over a plain background, and the page lands as soon as it knows its desktop. A window other than the pinned chat therefore starts loading without waiting for that desktop's layout.

If the shell cannot be read when the page opens (while it restarts, say), the phone shows home rather than a blank page, and lands where it was once the shell answers.

Coming back to the workspace after the phone slept reconnects at once, instead of waiting out a reconnect delay that can grow to 30 seconds. A connection that still looks open is checked and replaced if it does not answer. The shown page is no longer reloaded by a reconnect long after a return during which the connection held.

The page's built scripts and styles are cached by the browser for good, since their names change with their contents, so reopening the workspace no longer asks for them again.
