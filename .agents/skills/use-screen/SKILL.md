---
name: use-screen
description: Use when you need a display -- to run a program with a window (a GUI app, a game, an editor, a test that needs X), to see what is on the workspace's desktop, to take a screenshot of it, to type or click into a program there, or to hold the control lease while you drive it. The Screen is the workspace's one streamed Linux desktop, shown to the user as a window; the browser runs on it too (see the agentic-browser-fleet skill for driving Chromium).
metadata:
  author: imbue
---

# Using the Screen

The workspace has one Linux desktop, the **Screen**: an X server on display `:99` with the XFCE
window manager, streamed to the user's browser as one window of their desktop (the `screen` app,
"Screen" in the launcher). Any program that needs a display runs on it, the browser included. It
starts on first demand and never stops for emptiness; it ends only under memory pressure or when the
user quits it, and every program on it goes with it.

`uv run --no-sync screen` is your command for it, run from the repo root. It talks to the Screen
service (`http://127.0.0.1:8090`) and starts the Screen when it is down.

## The verbs

```bash
uv run --no-sync screen run <command...>       # start a program on the Screen (DISPLAY=:99), detached; prints its pid
uv run --no-sync screen windows                # the X windows: id, class, geometry, title
uv run --no-sync screen activate <window id>   # raise and focus a window
uv run --no-sync screen shot [--quality N] [--out PATH]   # a screenshot of the whole Screen (PNG by default)
uv run --no-sync screen size [WxH]             # read the Screen's size, or set it (16 px steps, 640x480 to 1920x1080)
uv run --no-sync screen control acquire        # take the control lease (starts the Screen when it is down)
uv run --no-sync screen control release        # give it back
uv run --no-sync screen control handoff "<reason>"   # hand the Screen to the human and stop
uv run --no-sync screen open [--show]          # open the Screen's window on the user's desktop (minimized unless --show)
uv run --no-sync screen browser open [URL]     # open the browser on the Screen (the fleet's one Chromium), URL as a new tab
```

`run` answers as soon as the program is started; a GUI program takes a moment to map its window,
so poll `windows` for it rather than trusting the first screenshot. Programs you start this way run
in the most expendable memory band, so under memory pressure they are shed before the Screen itself.

## The lease: the human always wins

The Screen has one controller at a time, a human or an agent. Take the lease before you drive, with
`screen control acquire` (or, for the browser, `agentic-browser-fleet acquire`, which is the same
lease):

- **`acquire` answers at once.** Exit 0 means the Screen is yours; 2 means a human holds it and you
  are queued to resume (tell the user, end your turn, and you will be messaged when it frees); 3
  means another agent holds it or the Screen is still starting (retry in a few seconds; for another
  agent you are queued and will be messaged).
- **Any human can take control at any moment**, from the strip above the Screen in their window or
  simply by using the Screen when nobody holds it. They always win: you are preempted, your lease is
  gone, and the Screen's input from you is no longer theirs to expect. **Stop, tell the user the
  human took the wheel, and end your turn.** Do not reclaim on your own; a bare "keep going" from the
  user is your cue to `acquire --reclaim`.
- **The lease goes idle after 60 seconds** without a `screen` command (a screenshot counts). Hold it
  by working, not by waiting; `release` it the moment you are done so the human has the Screen back.
- **`handoff "<reason>"`** gives the Screen to the human pinned (no other agent can take it until
  they give it back), shows them the Screen's window, and puts you first in line to resume: their
  first click or key claims it, and their "Give control back" (or leaving the Screen) hands it to
  you and messages you. Use it for anything only they can do (a password, a CAPTCHA, a judgement
  call), then end your turn.

Your X input lands whether or not you hold the lease (X knows nothing about leases); the lease is
what keeps a human's input from landing under yours and what tells you to stop. An agent that lost
the lease stops and ends its turn.

## Sending input

Two input paths, by what you drive:

- **Chromium**: the CDP proxy, through `playwright-cli` as the `agentic-browser-fleet` skill says.
  It is gated on the lease per frame, so a refused command there means the human took control.
- **Everything else**: `xdotool` against `DISPLAY=:99`, after `screen control acquire`:

```bash
export DISPLAY=:99
xdotool search --name "Calculator"          # window ids by title (or `screen windows`)
xdotool windowactivate --sync <id>          # or `uv run --no-sync screen activate <id>`
xdotool type --delay 20 "2+2"
xdotool key Return
xdotool mousemove 400 300 click 1
```

Take a `screen shot` after each step you cannot otherwise verify; the whole Screen is in it, so raise
the window you care about first (`screen activate`).

## Showing the user

`screen open` puts the Screen's window on the user's desktop, minimized by default so it waits in
their taskbar; `--show` restores and raises it. The user can also fill their viewport with it
(Fullscreen in the window menu) or hand the whole app window to it (Immersive, with the keyboard
locked). Like every window op, `open --show` changes what the user is looking at the moment it
returns, so do it when you want them to look, and never tell them to open a window you can show them
(the `manage-desktop` skill covers window placement).

## What to know

- One Screen, one size (at most 1920x1080). The controlling human's window drives its size; `screen
  size WxH` sets it as that window would, and a size the Screen cannot take is rounded.
- Programs started from the Screen itself (the XFCE menu, a terminal on it) run there too; the
  service raises them into the expendable band every ten seconds, so under pressure the programs go
  before the Screen does.
- The browser's tabs are checkpointed and come back when it is next started; everything else on the
  Screen is gone when the Screen ends, so do not leave work in a program's unsaved state.
- `screen windows` lists Chromium's windows beside the rest; a window whose class carries
  `tilion`, `chrome` or `fortress` is the browser's.
