- Choosing a provider for a chat whose account was signed out now switches the chat to it directly: the composer comes back with "Your next message switches this chat to ...", with no "Switch to ...?" dialog first. With the chat's own account gone there is no provider to stay on, so there was nothing for the dialog to ask.

- The switch dialog no longer says the agent "wraps up what it is doing" unless the chat is working. For an idle chat it says the agent hands the conversation over, and for a chat whose account was signed out (opened from the strip's "Change") it says the conversation moves to the new provider.

- A chat whose account was signed out no longer shows the model it last ran in the model bar. The bar stays empty (the composer's notice offers "Choose a provider") until a provider is chosen, and then shows the new one. On a phone the settings button stays.
