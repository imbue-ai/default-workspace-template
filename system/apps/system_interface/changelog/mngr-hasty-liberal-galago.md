Fixed a sign-in failure when an API key was submitted quickly after picking "Use an API key" under "Other ways to sign in".

Opening a provider's row starts that provider's primary sign-in, and picking a different method starts a second one. The key form renders straight away rather than waiting, but the modal went on holding the first sign-in until the second one came back -- so a key pasted and saved inside that window was submitted against the sign-in just left, which the workspace had already dropped and which takes a code rather than a key. The sign-in was rejected and the screen sat on "Saving" until it timed out.

The modal now holds no sign-in while the next one is being started, so Save stays unavailable for the moment it takes to arrive and the key can only ever be submitted against the sign-in it belongs to. The Enter key in the field is held to the same rule as the button.
