---
name: use-from-phone
description: Use whenever the user asks about reaching this workspace, its chats, or one of its apps from a phone, a tablet, another computer, or a web browser instead of the Imbue Studio desktop app, or about putting it on their phone's home screen. Walks them through turning on sharing, getting the link onto the phone, and adding it to the home screen on iPhone/iPad and Android.
metadata:
  author: imbue
---

# Using the workspace from a phone

There is no separate mobile app, and none is needed: the sharing URL opens the workspace in the phone's browser, and its desktop adapts to a phone screen.

Walk them through the steps below, in their words, skipping any they have already done.
You cannot do the first two for them: sharing is switched on, and the link sent, from Imbue Studio on their computer.

## 1. Turn on sharing

Check first: sharing is on while `ls data/.secrets/share.env` finds the file (the secrets guard refuses any other way of asking, so do not try one).
If it is off, tell the user:

1. In Imbue Studio, click the Share button (in the window's title bar, or inside the workspace).
2. Turn on "Enable sharing and web access".

Reassure them about what this does: it gives the workspace an address on the internet, and nobody but them can open it unless they explicitly grant someone access in that same panel.
The first time takes a minute or two while the panel sets up a secure link.

## 2. Get the link onto the phone

The simplest way, in the same Share panel: click "Email me a Mobile Access Link", then open that email on the phone and tap the button in it.
It always goes to the email address of their Imbue account.

Alternatively, the panel shows a link for the whole workspace and one for each app, each with a copy button; they can send that link to themselves however they like.
To give them the link to a single app yourself, use the `link` that `uv run --no-sync workspace-layout list` prints for it, which is its shared address once sharing is on.

On the phone, they sign in with the same Imbue account they use in Imbue Studio.

## 3. Add it to the home screen (optional)

This gives the workspace, or an app, its own icon that opens it full screen, like an installed app.
Have them open the link and sign in first, then:

**iPhone or iPad (Safari):**

1. Tap the Share button. On recent versions of iOS it is inside the "..." menu next to the address bar.
2. Scroll down and tap "Add to Home Screen".
3. Leave "Open as Web App" on, if the switch is shown, so it opens in its own window without the browser around it.
4. Tap "Add".

**Android (Chrome):**

1. Tap the three-dot menu at the top right.
2. Tap "Add to Home screen" (on some phones it reads "Install app").
3. Confirm the name and tap "Add" (or "Install").

Other browsers have the same option under a similar name in their share or main menu.

## When it does not work

- The link does not open: sharing is off, or still being set up. Have them check the Share panel, which says which.
- The Share panel says links shared before no longer work: the address changed, so old links and home-screen icons are stale. Send a fresh link and re-add the icon.
- A workspace that runs on their own computer is reachable only while that computer is on and awake. One on Imbue Cloud stays reachable either way.
