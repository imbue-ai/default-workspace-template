The workspace has a phone layout. On a screen whose shorter side is under 700px (any phone, upright or on its side, but no iPad), the squeezed desktop is gone and the workspace shows one thing at a time:

- A bar at the bottom: home, a pill naming what is on screen (the avatar and "Chat" for the chat window, the workspace's name on the home screen) with how many other windows are open, and a plus. It clears the home indicator when saved to the home screen and sits right on the browser's toolbar in a browser tab.

- A home screen of every app on the desktop's wallpaper. Tapping an app shows its window, or opens one if it has none; holding it offers the app's ways to start something, so a second terminal is one gesture away.

- Tapping the pill lists every window of every desktop, the ones you looked at most recently first. Each can be shown, closed with its X, or given a Refresh (and Quit, for an app that can be stopped) from its menu; Close all closes everything but the chat after asking. Holding the pill opens the menu of the window on screen.

- The plus opens the launcher as a list you tap: apps to open, and with text typed, the windows that match and "Send to chat..." / "Draft into chat". Sending a message shows the chat it went to.

- A phone moves nothing on a laptop: showing a window there changes nobody's arrangement, and a window opened from the phone waits minimized at the bottom of the laptop's taskbar. When an agent shows or opens a window for the phone, the phone switches to it; when a window it shows is closed anywhere, it goes back home.

- Reloading, or coming back to the page later, lands where you left off.

- Saved to a phone's home screen, the workspace is titled with its name and wears its avatar as the icon.

On every layout, an operation that fails now says so in a short note at the bottom of the screen that goes away on its own, instead of a browser alert.

Agents reading the workspace's clients see each one's `shown_history`, and `layout.py` `open`, `focus`, and `show` now tell the client they target which window they brought forward.

An agent's window now appears where the user is looking when they have moved to another browser since messaging the agent (a phone's browser tab and the same workspace saved to its home screen are two browsers): an op with no `--client` passes over a messaging client that is no longer connected in favour of the one connected client, and still goes to it when no other client, or several, are connected.
